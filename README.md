# Casanode-OS

This Git repository allows you to generate a Casanode image installable on a Raspberry Pi compatible with the arm64 architecture.

## Prerequisites

Before starting, ensure you have the following:

1. A Raspberry Pi with a 64-bit operating system.
2. The necessary dependencies related to the [pi-gen repository](https://github.com/RPi-Distro/pi-gen). Install them by running the following command:

	```bash
	apt-get install coreutils quilt parted qemu-user-static debootstrap zerofree zip \
	dosfstools libarchive-tools libcap2-bin grep rsync xz-utils file git curl bc \
	gpg pigz xxd arch-test
	```

### Additional OS Prerequisites

- Support for 64-bit architecture.
- Equipped with a Bluetooth antenna.
- Has an Ethernet port or a Wi-Fi card.

### Supported Devices

This Casanode image supports the following Raspberry Pi models:

- Raspberry Pi 3 Model B
- Raspberry Pi 3 Model B+
- Raspberry Pi 3 Model A+
- Raspberry Pi 4 Model B
- Raspberry Pi 400
- Raspberry Pi 5

We encourage you to test on other devices and provide feedback.

## Building the Image

Once the dependencies are installed, you can proceed with creating the image.

1. Run the build.sh script to create the image:

	```bash
	bash build.sh
	```

The image generation will take between one and two hours. Once the process is complete, the image will be available in the `pi-gen/deploy/` folder.

### Optional Parameters

You can customize the image generation process using the following optional parameters:

- `compression`: Possible values are `zip`, `gz`, `xz`. This parameter allows you to generate a compressed image.
- `deb-version`: This parameter lets you specify the version of the Debian package available on the repository [https://sentinelgrowthdao.github.io/casanode-api/](https://sentinelgrowthdao.github.io/casanode-api/) to use during the build.

## Using the Image

After generating the image, use one of the following scripts to install the OS on an SD card:

- `tools/prepare_sdcard.py`: Clones the image to an SD card and allows pre-configuration of Wi-Fi, SSH, and other settings.
- `tools/create-img.sh`: Patches the image with Wi-Fi settings and prepares it for SD card installation.

### tools/prepare_sdcard.py

These helpers allow you to install the generated OS onto an SD card and pre-configure it for seamless deployment.

Install the Python dependencies once before running the helper:
```
pip install -r requirements.txt
```
Usage:
```
sudo python3 tools/prepare_sdcard.py deploy/2025-10-05-casanode-os.img --output-image /dev/sda --enable-ssh-eth0
```
Key features:
- Clones an input .img to a file or block device, then patches Wi-Fi credentials, regulatory domain, and API auth token.
- Generates Wi-Fi and browser QR codes plus a `device.json` summary under `sdcard/<ssid>/`.
- Offers CLI overrides for SSID, password, country, auth token, IP/port, and can drop the `enable-ssh-eth0` marker.

Run the script without `--output-image` to clone the image into `sdcard/<ssid>/` automatically.

### tools/create-img.sh

This script patches a base image with fixed values for the wifi access point (SSID and password).

Usage:
```
sudo ./create-img.sh <base-image.img> [OUTPUT.img] [COUNTRY] [SSID] [PASS]
```
Patches the image file directly (loop-mount) with the same Wi-Fi + country data, enforcing passphrase length.

Once the image is patched, you can install the OS on an SD card using the `dd` command. This will write the patched image directly to the SD card.

Example command:
```
sudo dd if=<patched-image> of=<sd-card-device> bs=4M conv=fsync status=progress
```

Replace `<patched-image>` with the path to the patched image file (e.g., the output from `tools/create-img.sh`), and `<sd-card-device>` with the device path of your SD card (e.g., `/dev/sda`). Ensure the SD card is not mounted before running the command.

## Support and Troubleshooting

For any questions or issues not covered in the documentation, please open an issue on the GitHub repository.

### Note on Docker Rootless Mode

If you encounter issues with pi-gen while Docker is in rootless mode, you can disable rootless mode and restart Docker in root mode:

1. Disable rootless mode:
	```bash
	systemctl --user stop docker
	```

2. Restart Docker in root mode:
	```bash
	sudo systemctl restart docker
	```

## License

This project is licensed under the GPL v3 License - see the LICENSE file for details.
