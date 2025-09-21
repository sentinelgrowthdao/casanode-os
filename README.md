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
- `deb-version`: This parameter lets you specify the version of the Debian package available on the repository [https://sentinelgrowthdao.github.io/casanode-ble/](https://sentinelgrowthdao.github.io/casanode-ble/) to use during the build.

## Using the Image

After generating the image, follow these steps to install it on your Raspberry Pi:

1. Download and install the Raspberry Pi Imager software available [here](https://www.raspberrypi.com/software/).
2. Launch Raspberry Pi Imager.
3. Select the type of hardware.
4. Choose the generated image by selecting "custom image" at the bottom of the list.
5. Insert the SD card into your computer and click on "Choose Storage" to select it.
6. You can set custom options such as a custom user, hostname, or Wi-Fi connection.
7. Once the SD card is ready, insert it into your Raspberry Pi and power it on.

After a few minutes, your Casanode will be ready to use.

### Wi-Fi Access Point

To avoid Wi-Fi being blocked by `rfkill`, provide a two-letter country code in the `device.json` file located on the boot
partition. An example configuration is available in `device.json.example`.

## Configuring Casanode

To connect to Casanode, use the Android application available in this repository: [casanode-mobile-app](https://github.com/sentinelgrowthdao/casanode-mobile-app).

With this application, you can interact with your Casanode and finalize its configuration to make it fully operational.

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

## Quick Wi-Fi / SSH configuration helpers

Two helper scripts let you patch an existing image or an SD card without rebuilding:

### install-sdcard.sh

Usage:
```
sudo ./install-sdcard.sh <image-file> <sd-card-device> [COUNTRY] [SSID] [PASS]
```
Parameters:
- COUNTRY: 2-letter ISO code (default FR)
- SSID: Access Point SSID (default Casanode-alpha1)
- PASS: WPA2 passphrase (8–63 chars). If omitted you'll be prompted (default fallback stored internally).

Features:
- Validates passphrase length.
- Writes boot/casanode/device.json consumed by first-boot logic.
- Optionally creates /boot/enable-ssh-eth0 to allow SSH on ethernet (firewall controlled).
- Updates hostapd.conf (if already present) to reflect SSID/PASS so first boot immediately matches config.

### create-img.sh

Usage:
```
sudo ./create-img.sh <base-image.img> [OUTPUT.img] [COUNTRY] [SSID] [PASS]
```
Patches the image file directly (loop‑mount) with the same Wi‑Fi + country data, enforcing passphrase length.

### Firewall behavior

`casanode-firewall.sh`:
- Accepts all traffic on wlan0 (explicit extra ACCEPT for tcp/80 for clarity).
- On eth0, SSH (tcp/22) allowed only if one of:
	* /boot/enable-ssh-eth0 exists
	* /boot/firmware/enable-ssh-eth0 exists
	* Environment variable CASANODE_ALLOW_ETH0_SSH=1 when the firewall script runs
- Then everything else on eth0 is dropped.

### Security note
Change the default Wi‑Fi passphrase before distributing devices. Consider adding a random generator pipeline later.

## License

This project is licensed under the GPL v3 License - see the LICENSE file for details.
