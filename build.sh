#!/bin/bash

set -euo pipefail

PI_GEN_DIR=./pi-gen
CASANODE_DIR=./casanode
IMAGE_PATH=""

# Variables for parameters
COMPRESSION=""
INSECURE=""
COMMIT_HASH="37f9ba6e3e8e6d12f5cdfc3335926d83abe9de38"


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

# Copy the casanode configuration to the pi-gen configuration
cp "${CASANODE_DIR}/config" "${PI_GEN_DIR}/config" || error_exit "Failed to copy config file."

# Skip stages to build a lite system
for STAGE in stage3 stage4 stage5; do
	touch "${PI_GEN_DIR}/${STAGE}/SKIP" || error_exit "Failed to create ${STAGE}/SKIP."
done

touch "${PI_GEN_DIR}/stage4/SKIP_IMAGES" || error_exit "Failed to create stage4/SKIP_IMAGES."
touch "${PI_GEN_DIR}/stage5/SKIP_IMAGES" || error_exit "Failed to create stage5/SKIP_IMAGES."

# Add Casanode installation files to pi-gen
rsync -avg --delete --exclude="config" "${CASANODE_DIR}/" "${PI_GEN_DIR}/stage2/04-casanode/" || error_exit "Failed to copy casanode files."
# Make the 00-run.sh script executable
chmod +x "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to make 00-run.sh executable."
# Replace <commit-hash> inside 00-run.sh with the latest commit hash
sed -i "s/<commit-hash>/${COMMIT_HASH}/" "${PI_GEN_DIR}/stage2/04-casanode/00-run.sh" || error_exit "Failed to replace commit hash in 00-run.sh."


# Build the pi-gen image
cd "${PI_GEN_DIR}/" || error_exit "Failed to change directory to ${PI_GEN_DIR}."
CLEAN=1 bash build-docker.sh || error_exit "Failed to build the pi-gen image."

# Check if the image was created successfully
IMAGE_PATH=$(ls deploy/*.img | tail -n 1) || error_exit "Image creation failed or image not found."
if [ -f "$IMAGE_PATH" ]; then
	echo "Image created successfully: $IMAGE_PATH"
else
	error_exit "Image creation failed or image not found."
fi

# Remove the build container
if [ "$(sudo docker ps -a -q -f name=pigen_work)" ]
then
	sudo docker rm -v pigen_work || error_exit "Failed to remove the build container."
else
	echo "Container pigen_work does not exist."
fi

# Move deploy directory to casanode directory
if [ -d "./deploy" ]
then
	# If the deploy directory already exists, remove it
	[ -d "../deploy" ] && rm -rf ../deploy
	# Move the deploy directory to the casanode directory
	mv ./deploy/ "../" || error_exit "Failed to move deploy directory."
	
	echo -e "\e[32mBuild completed successfully, you can find the image in deploy directory.\e[0m"
else
	error_exit "Deploy directory not found, build failed."
fi
