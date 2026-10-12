# HY300 Lite (fenix)

The **HY300 Lite** (or HY300 RK3326/clone) is a hardware and software research and reverse engineering project on a low-cost projector.

The idea is to understand how this device works, document its quirks, and explore what can be done with the hardware and software it uses.

## About the Project

This repository gathers documentation, reference files, original tools, and investigations of the platform. It serves as a space to preserve findings, record experiences, and develop new uses for this device.

> [!NOTE]
> The project is still in the research phase. It is not yet a ready-to-use Linux distribution (or kernel), just investigation and early testing.

## About the Device

The public name adopted in this project is **HY300 Lite**, while **fenix** is the codename used for the device's internal identification.

The device under investigation is sold within a category of projectors that often share similar names and looks, but usually use very different hardware platforms.

The names fenix and HY300 Lite are conventions adopted to make identification easier during the research. They are not official names created by the manufacturer.

## Goal

The goal is to investigate how the device works at different levels, from the original Android and its proprietary components to the firmware, the boot chain, and the Linux kernel.

The research also aims to preserve information that may be useful for future modifications, system recovery, and the development of components specific to this platform.

Since the project involves reverse engineering, not every analysis will be correct from the start. Conclusions may change as new evidence is found.

## Repository Structure

- [`docs/`](docs/): documentation, findings, procedures, and research notes.
- [`firmware/original/`](firmware/original/): firmware images preserved as a reference for analysis and recovery.
- [`firmware/fenix/`](firmware/fenix/): firmware components prepared or modified specifically for the device.
- [`kernel/`](kernel/): kernel-related work and source code.
- [`uboot/`](uboot/): work related to U-Boot and the boot chain.
- [`tools/`](tools/): original tools developed to assist the investigations.

The directories may evolve as the project advances and new needs come up, but this is the current base as of the creation of this Markdown file.

## Project Status

fenix is an ongoing research project. Its focus is to document the investigation process, preserve evidence, and explore development possibilities.

> [!WARNING]
> The information documented for this hardware should only be used as inspiration if your hardware doesn't match the essential components of this project. So before using it as a compatible base, check whether your hardware matches the one used in this research.

## Licensing

The repository has different licensing terms, depending on the type and origin of the content.

- The documentation in `docs/` is made available under the Creative Commons Attribution-ShareAlike 4.0 International license (CC BY-SA 4.0).
- The original tools developed for the project in `tools/` are made available under the Apache License 2.0.
- The original firmware images in `firmware/original/` are not automatically covered by this repository's licenses and remain the property of their respective rights holders.
- Software components derived from existing projects keep the licenses that apply to their origins. Binary blobs and other third-party components remain subject to their own terms.

The `LICENSE` and `NOTICE` files in the relevant directories contain additional information about these terms.

## Disclaimer

This is an independent project, with no official affiliation with Magcubic, Rockchip, or any other manufacturer mentioned in the documentation.

> [!CAUTION]
> Analyzing and modifying firmware can cause data loss, boot failures, or leave a device unusable. The materials in this repository are intended for research and development, so any damage will be **YOUR** responsibility.
