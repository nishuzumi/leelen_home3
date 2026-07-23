# Leelen Home

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1.0-blue.svg)](https://www.home-assistant.io/)

Home Assistant integration for Leelen (立林) smart home devices.  

## Features

- **Climate**: Central air conditioner and floor-heating control with current
  temperature and humidity, target temperature, mode, and fan-speed state
- **Fan**: Fresh-air system control
- **Sensor**: Temperature and humidity sensors exposed by every discovered
  thermostat panel
- **Live state**: Optional MQTT state pushes with REST used for the initial
  snapshot, disconnected fallback, and control confirmation

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant
2. Go to "Integrations" → Click "+" button
3. Search for "Leelen Home" or add as custom repository:
   - Repository: `https://github.com/nishuzumi/leelen_home3`
   - Category: Integration
4. Click "Download"
5. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/leelen_home3` folder to your Home Assistant `config/custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "Leelen Home"
4. Enter your phone number and verification code
5. Select the devices you want to add

To enable real-time state updates, open the integration options and enter the
MQTT Client ID and username already registered by the Leelen app. Both values
are required; leaving both empty keeps REST fallback sync enabled.

## Supported Devices

| Home Assistant platform | Leelen logical service | Service type |
|---|---|---:|
| climate | Central air conditioner, including current temperature and humidity | 8259 |
| climate | Floor heating, including current temperature and humidity | 8268 |
| fan | Fresh-air system | 8261 |
| sensor | Thermostat temperature and humidity sensors | 8272 |

## Troubleshooting

### Cannot connect to gateway

- Ensure your Home Assistant can access the Leelen cloud service
- Check if your account is bound to a gateway device in the Leelen app

### Devices not showing up

- Try refreshing devices from the integration options
- Check if devices are online in the Leelen app

## Support

If you encounter any issues, please [open an issue](https://github.com/snailll2/leelen_home3/issues) on GitHub.

## License

This project is licensed under the MIT License.
