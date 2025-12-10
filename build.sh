#!/bin/bash

set -euo pipefail

PI_GEN_DIR=./pi-gen
CASANODE_DIR=./casanode
SENTINEL_IMAGE="ghcr.io/sentinel-official/sentinel-dvpnx:latest"
SENTINEL_TAR_ARM64="${CASANODE_DIR}/files/docker/sentinel-dvpnx-arm64.tar"
SENTINEL_TAR_AMD64="${CASANODE_DIR}/files/docker/sentinel-dvpnx-amd64.tar"
IMAGE_PATH=""

# Variables for parameters
COMPRESSION=""
DEB_VERSION=""
SENTRY_DSN=""
DEVICE_JSON=""

# Load the .env file if it exists
if [ -f .env ]
then
	source .env
fi

# Process script arguments
while [[ "$#" -gt 0 ]]
do
	case "$1" in
		--compression=*)
			COMPRESSION="${1#*=}"
			if [[ ! "$COMPRESSION" =~ ^(zip|gz|xz)$ ]]
			then
				error_exit "Invalid value for compression. Valid options are zip, gz, xz."
			fi
			shift
			;;
		--deb-version=*)
			DEB_VERSION="${1#*=}"
			shift
			;;
		--sentry-dsn=*)
			SENTRY_DSN="${1#*=}"
			shift
			;;
		--device-json=*)
			DEVICE_JSON="${1#*=}"
			shift
			;;
		*)
			error_exit "Unknown parameter passed: $1"
			;;
	esac
done

# Function to log errors
error_exit()
{
	echo -e "\e[31mError: $1\e[0m"
	exit 1
}

# Function to initialize and update the pi-gen submodule
initialize_pi_gen_submodule()
{
	git submodule init || error_exit "Failed to initialize submodule."
	git submodule update || error_exit "Failed to update submodule."
}

prepare_sentinel_image_tar()
{
	mkdir -p "$(dirname "${SENTINEL_TAR_ARM64}")"

	if ! command -v docker >/dev/null 2>&1; then
		echo "[build] Docker not available on host; skipping Sentinel image packaging."
		rm -f "${SENTINEL_TAR_ARM64}" "${SENTINEL_TAR_AMD64}" >/dev/null 2>&1 || true
		return 0
	fi

	# Pull and save ARM64 version
	echo "[build] Pulling ${SENTINEL_IMAGE} for arm64..."
	if docker pull --platform linux/arm64 "${SENTINEL_IMAGE}"; then
		tmp_tar="${SENTINEL_TAR_ARM64}.tmp"
		if docker save -o "${tmp_tar}" "${SENTINEL_IMAGE}"; then
			mv "${tmp_tar}" "${SENTINEL_TAR_ARM64}"
			echo "[build] Saved ${SENTINEL_IMAGE} (arm64) to ${SENTINEL_TAR_ARM64}"
		else
			echo "[build] Failed to save ${SENTINEL_IMAGE} (arm64) to ${SENTINEL_TAR_ARM64}" >&2
			rm -f "${tmp_tar}" >/dev/null 2>&1 || true
		fi
	else
		echo "[build] Failed to pull ${SENTINEL_IMAGE} for arm64; skipping." >&2
	fi

	# Pull and save AMD64 version
	echo "[build] Pulling ${SENTINEL_IMAGE} for amd64..."
	if docker pull --platform linux/amd64 "${SENTINEL_IMAGE}"; then
		tmp_tar="${SENTINEL_TAR_AMD64}.tmp"
		if docker save -o "${tmp_tar}" "${SENTINEL_IMAGE}"; then
			mv "${tmp_tar}" "${SENTINEL_TAR_AMD64}"
			echo "[build] Saved ${SENTINEL_IMAGE} (amd64) to ${SENTINEL_TAR_AMD64}"
		else
			echo "[build] Failed to save ${SENTINEL_IMAGE} (amd64) to ${SENTINEL_TAR_AMD64}" >&2
			rm -f "${tmp_tar}" >/dev/null 2>&1 || true
		fi
	else
		echo "[build] Failed to pull ${SENTINEL_IMAGE} for amd64; skipping." >&2
	fi
}

# Check if required directories exist
[ -d "${CASANODE_DIR}" ] || error_exit "Casanode directory not found."

# Ask user if they want to reset the pi-gen submodule
if [ -d "${PI_GEN_DIR}" ]; then
	read -p "pi-gen directory already exists. Do you want to reset it to the committed state? (y/n): " choice
	case "$choice" in 
		y|Y ) 
			initialize_pi_gen_submodule
			;;
		n|N )
			echo "Continuing with existing pi-gen directory."
			;;
		* )
			error_exit "Invalid choice. Exiting."
			;;
	esac
else
	initialize_pi_gen_submodule
fi

# Check if the configuration file exists
[ -f "${CASANODE_DIR}/config" ] || error_exit "Casanode config file not found."


# Check that the Casanode version is properly defined
if [[ -z "${DEB_VERSION}" ]]; then
	error_exit "The --deb-version parameter is required to specify the Casanode version."
fi

# Copy the casanode configuration to the pi-gen configuration
cp "${CASANODE_DIR}/config" "${PI_GEN_DIR}/config" || error_exit "Failed to copy config file."

# If the "compression" parameter is passed with a value of "zip" or "gz" or "xz"
if [[ -n "$COMPRESSION" ]]
then
	sed -i "s/DEPLOY_COMPRESSION=\"none\"/DEPLOY_COMPRESSION=\"$COMPRESSION\"/" "${PI_GEN_DIR}/config" || error_exit "Failed to set compression type in config."
else
	sed -i "s/DEPLOY_COMPRESSION=.*$/DEPLOY_COMPRESSION=\"none\"/" "${PI_GEN_DIR}/config" || error_exit "Failed to set compression type in config."
fi

# Skip stages to build a lite system
for STAGE in stage3 stage4 stage5; do
	touch "${PI_GEN_DIR}/${STAGE}/SKIP" || error_exit "Failed to create ${STAGE}/SKIP."
done

touch "${PI_GEN_DIR}/stage4/SKIP_IMAGES" || error_exit "Failed to create stage4/SKIP_IMAGES."
touch "${PI_GEN_DIR}/stage5/SKIP_IMAGES" || error_exit "Failed to create stage5/SKIP_IMAGES."

# Ensure Sentinel docker image tarball is prepared before syncing Casanode files
prepare_sentinel_image_tar

# Add Casanode installation files to pi-gen
rsync -avg --delete --exclude="config" "${CASANODE_DIR}/" "${PI_GEN_DIR}/stage2/04-casanode/" || error_exit "Failed to copy casanode files."
if [[ -n "$DEVICE_JSON" ]]; then
	mkdir -p "${PI_GEN_DIR}/stage2/04-casanode/files"
	cp "$DEVICE_JSON" "${PI_GEN_DIR}/stage2/04-casanode/files/device.json" || error_exit "Failed to copy device.json."
fi
# Make the 00-run.sh script executable
chmod +x "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to make 00-run.sh executable."
# Replace <deb-version> inside 00-run.sh
sed -i "s/<deb-version>/${DEB_VERSION}/g" "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to replace deb-version hash in 00-run.sh."
# Replace <sentry-dsn> inside 00-run.sh
sed -i "s|<sentry-dsn>|${SENTRY_DSN}|" "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to replace sentry-dsn hash in 00-run.sh."

# If SENTRY_DSN is set in the environment
if [ -n "${SENTRY_DSN}" ]
then
	sed -i "s|^SENTRY_DSN=\".*\"$|SENTRY_DSN=\"${SENTRY_DSN}\"|" "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to set SENTRY_DSN in 00-run.sh."
fi

# Build the pi-gen image
cd "${PI_GEN_DIR}/" || error_exit "Failed to change directory to ${PI_GEN_DIR}."
CLEAN=1 bash build-docker.sh || error_exit "Failed to build the pi-gen image."

# Check if the image was created successfully
IMAGE_EXTENSIONS=("*.img" "*.zip" "*.xz" "*.gz")
IMAGE_PATH=""

# Find the image file
for ext in "${IMAGE_EXTENSIONS[@]}"
do
	IMAGE_PATH=$(find deploy -type f -name "$ext" | head -n 1)
	if [ -n "$IMAGE_PATH" ]; then
		break
	fi
done

# Check if the image was created successfully
if [ -f "$IMAGE_PATH" ]; then
	echo -e "\033[0;32mImage created successfully: $IMAGE_PATH\033[0m"
else
	error_exit "Image creation failed or image not found."
fi

# Create the ../deploy directory if it doesn't exist
[ -d "../deploy" ] || mkdir -p ../deploy

# Extract the base name and modify it
BASE_NAME=$(basename "$IMAGE_PATH")
NEW_NAME=$(echo "$BASE_NAME" | sed 's/^image_//' | sed 's/-lite//')

# Move the image to the deploy directory
mv "$IMAGE_PATH" "../deploy/$NEW_NAME" || error_exit "Failed to move image to deploy directory."
echo -e "\e[32mBuild completed successfully, you can find the image in the deploy directory.\e[0m"

# Remove the build container
if [ "$(sudo docker ps -a -q -f name=pigen_work)" ]
then
	sudo docker rm -v pigen_work || error_exit "Failed to remove the build container."
else
	echo "Container pigen_work does not exist."
fi
