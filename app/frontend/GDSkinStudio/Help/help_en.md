# About this app {#about}

GD Skin Studio is a Windows app that makes Grim Dawn MODs that turn AI-made 3D models into illusion equipment. It generates a 3D model from an image (or text), converts it for Grim Dawn's male and female characters, builds a MOD that you can use as a head illusion, and installs it into the game's `mods` folder.

## What it does

- **Create a 3D model from an image**: Creates a 3D model with Microsoft's TRELLIS from an image of one character showing the whole body. Generation runs on this PC's NVIDIA GPU. You can also add back and side views (up to 4 images). If the image has a background, it is removed automatically before generation.
- **Create a 3D model from text (optional)**: Creates a 3D model from English text. This requires downloading "Optional items (text → 3D)" in the setup and accepting the CLIP notice.
- **Convert a GLB file**: Converts a GLB file made with another tool without generating anything. No GPU is needed for this.
- **Convert for Grim Dawn**: Converts the 3D model into the game's full-body meshes and textures for male and female characters. Pose and body fitting, vertex count adjustment, and bone assignment are automatic.
- **Check and save**: Rotate and inspect the generated model and the male and female results on the "3D preview" tab, then give the equipment a name (English and Japanese) and save it to the Library.
- **Build and install the MOD**: Builds one MOD from all equipment in the Library and installs it into Grim Dawn's `mods` folder. The game's original files are not changed. Before installing, the files in the `mods` folder that will be replaced are backed up, and "Uninstall…" returns the folder to its state before the first install.
- **Use it in the game**: Buy your equipment's illusion from the Quartermaster in Devil's Crossing to add it to your collection, then apply it to your head slot at the Illusionist. Invisible (transparent) illusions for the chest, legs, hands, feet, shoulders, and medal are added to your collection at the same time (how the medal looks when the illusion is applied has not been confirmed in the game).
- **Share with others**: You can create equipment files to exchange with other users (`.gdskin`) and MOD share files that hand over your MOD as is for multiplayer (`.gdskinmod`). Multiplayer behavior has not been tested.

## What you need

- You need Windows 10 / 11 (64-bit) and Grim Dawn (Steam version) with the Ashes of Malmouth expansion.
- 3D generation from images and text needs an NVIDIA GPU (12 GB or more of GPU memory, 16 GB or more recommended). The first-run setup downloads the TRELLIS runtime and models from the internet (about 8.6 GB to download, about 13 GB on disk afterward).
- Even without a GPU, you can convert GLB files made with other tools and build and install MODs. Some GLB files need a recent Visual C++ Redistributable for the conversion (see [Installing and updating the app](#install)).

> The app has been tested on only one PC: a GeForce RTX 5090 Laptop GPU (24 GB) with Windows 11 (Japanese). Generation on GPUs with 12 to 16 GB of GPU memory has not been tested.

For the full requirements and the installation steps, see [Installing and updating the app](#install).

## Your data and internet connections

- The images and text you enter are never sent outside this PC. 3D generation also runs on this PC's GPU.
- The app connects to the internet only when the setup downloads the TRELLIS runtime and models from their sources (the first download, and re-verifying or reinstalling with "TRELLIS runtime management (re-verify, reinstall, remove)" in "Settings").
- During 3D generation, missing files are not downloaded; generation stops with an error. Turning on "Block network access" in "Advanced settings" under "Generation settings" refuses outgoing connections from the generation process (Python); connections that native code opens by itself are not covered.
- The log does not contain the contents of the text and images you enter or your user folder name.
- Your equipment, MOD builds, install records, settings, and logs are stored in the data folder on this PC (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`; see [Where the app keeps its data](#files)).

## The screens

Use the menu on the left to switch screens.

- **"Generate"**: Create a 3D model from an image, text, or a GLB file, convert it for male and female characters, and save it as equipment (see [The "Generate" screen](#generate)).
- **"Library"**: Check your saved equipment, edit names, regenerate, reconvert, or delete it, and export or import equipment (see [The "Library" screen](#library)).
- **"MOD"**: Build a MOD from the equipment in the Library, and install it into Grim Dawn or uninstall it. Sharing for multiplayer is also done here (see [The "MOD" screen](#mod)).
- **"Log"**: View a record of operations and processing (see [The "Log" screen](#log)).
- **"Setup"**: Download the TRELLIS runtime and models used for 3D generation and prepare the game side. Until you finish the setup or choose "Do it later", this screen opens when you start the app (see [The "Setup" screen](#setup)).
- **"Settings"**: Change settings such as the Grim Dawn install folder, reference meshes, data, and display language; check data usage and clean up; manage the TRELLIS runtime; and check the environment (see [The "Settings" screen](#settings)).
- **"Help"**: This help. Choose a topic in "Contents" on the left. Click a link in the text to go to that topic. When you type in "Search the help", only the topics that contain the text stay in the contents, and the matches are highlighted in the text ("Clear" shows all topics again). Press F1 on any screen to open the help. On the help screen, Ctrl+F moves to the search box. The help can be read even while the backend is not running.

The default display language is Japanese. To switch it, choose 日本語 or English in "Display language" on the "Settings" screen, click "Save settings", and restart the app.

### Status at the bottom left

- Below the menu on the left, the app shows the status of the backend (the process that runs generation, conversion, and MOD builds). "Backend: connected" (green mark) means the app is ready. Right after the app starts, "Starting the backend…" is shown.
- When "Backend: stopped" or "Backend: cannot start" (red mark) is shown, a bar at the top of the screen shows the reason and a "Restart backend" button. Even while "Backend: connected" is shown, the same bar shows a message when the preparation after startup (such as the environment check) or a page refresh fails ("Restart backend" cannot be clicked then). If the backend keeps stopping, see [Troubleshooting](#troubleshooting).
- Below that, the name of the NVIDIA GPU that was found is shown. If none is found, "No GPU found" is shown.

## Closing the app

- If tasks are running or waiting (generation, MOD builds, exports, setup downloads, etc.), the app asks before closing. If you choose "Cancel and close", these tasks are cancelled and the app closes (a setup download can be resumed where it left off the next time you start the app).
- While a MOD install, uninstall, or restore, or an equipment import is running, stopping it partway would leave the `mods` folder or the Library in an incomplete state. If you choose "Wait and close", the app waits for it to finish and then closes (waiting tasks and cancellable tasks such as generation are cancelled; "Don't close" keeps the app open). If the task has not finished after 2 minutes, or the backend does not respond, you can choose "Close anyway" or "Keep waiting". If you close anyway, the `mods` folder or the Library may be left in an incomplete state; in that case, install again from the "MOD" screen the next time you start the app.

## Unofficial tool and licenses

- This app is an unofficial fan-made tool. It is not affiliated with Crate Entertainment (the developer of Grim Dawn), Microsoft (the developer of TRELLIS), or any other developer or provider of the software, models, and services this app uses, and it is not approved or endorsed by them. Grim Dawn is a trademark of Crate Entertainment.
- This app itself is provided under the MIT License. "License (MIT)" in "About" on the "Settings" screen shows the full text (the same as `LICENSE.txt` in the extracted folder).
- Third-party software that is bundled or downloaded is covered by its own license, not the MIT License. "Third-party licenses" in "About" shows the list and the licenses, which are also in `THIRD_PARTY_NOTICES.en.txt` and `licenses\` in the extracted folder.
- The full license texts of what the setup downloads can also be checked on the "Setup" screen and with "Full license" in "Setup (installed items and licenses)" in "Settings".
- No Grim Dawn files are bundled. The reference meshes and the MOD template are read from your game.

## Please note

- Install MODs at your own risk. The original files are backed up before installing, but backing up your saves separately is recommended (see [Installing a MOD and using it in the game](#mod-install)).
- For 3D generation, use only images that you have the right to use. Publishing or distributing models made from images of other people's works or characters may infringe the rights of the rights holders.
- The quality, shape, and content of the generated 3D models are not guaranteed. You are responsible for the images and text you enter and for publishing or distributing the models and MODs you make.
- This app is provided as is, without warranty of any kind, express or implied.

# How it works, step by step {#flow}

When you use the app for the first time, follow these steps. Each step links to the section with the details.

1. **Start the app**: Start `GDSkinStudio.exe` in the extracted folder (see [Installing and updating the app](#install)).
2. **Do the first-run setup**: At the first start, the "Setup" screen opens. Go through it in order with "Next". In "2. Components and consent", check the licenses and agree, then click "Start download" to download the TRELLIS runtime and models (about 8.6 GB; about 10 minutes to 1 hour depending on your connection). In "4. Prepare reference meshes", click "Prepare from the game", and finally click "Finish and start generating" (see [The "Setup" screen](#setup)).
  - Until you finish the setup or choose "Do it later", this screen opens every time you start the app.
  - If you have no GPU, follow **Without a GPU (from a GLB file)** below instead.
3. **Create a 3D model**: On the "Generate" screen, choose "Image", "Text", or "GLB file (advanced)" under "Input", specify the image or other input, and click "Start generation". The app generates a 3D model and converts it for male and female characters (about 1 to 2 minutes). For a GLB file, it only converts the file without generating anything (see [The "Generate" screen](#generate)).
4. **Check the result**: In "Preview", switch between "Generated", "Male (converted)", and "Female (converted)" and rotate them to inspect them. If the front faces backwards, click "Rotate 180° and reconvert". If the pose or a weapon bothers you, you can remake the model with "Regenerate with another seed" or with "Use this candidate" on the "Candidates" tab.
5. **Save it as equipment**: Enter "In-game name (English)" (required) and "Japanese name (optional)", then click "Save as equipment". The equipment is added to the Library, and the "Library" screen opens (see [The "Library" screen](#library)).
6. **Build the MOD**: Click "MOD" on the left, then click "Build MOD". One MOD is built from all equipment in the Library (see [The "MOD" screen](#mod)).
7. **Install it to Grim Dawn**: Exit Grim Dawn, then click "Install to Grim Dawn…". Check the changes in the confirmation window and click "Install"; the MOD is placed in Grim Dawn's `mods` folder (see [Installing a MOD and using it in the game](#mod-install)).
  - Before you install for the first time, it is recommended to back up your game saves.
8. **Set the Steam launch option**: Set `/basemods` in the Steam launch options. This app does not change the launch options, so set it in Steam yourself. "How to set /basemods" on the "MOD" screen shows the steps (see [Installing a MOD and using it in the game](#mod-install)).
9. **Use it in the game**: Start Grim Dawn from Steam, buy "Illusion: <name>" from the Quartermaster (Ellis) in Devil's Crossing (Survivors reputation of Friendly or higher), close the shop, and right-click it to add it to your collection. At the Illusionist, apply your equipment's illusion to the head and invisible (transparent) illusions to the other slots (see [Installing a MOD and using it in the game](#mod-install)).
  - Illusions can only be applied to equipped slots. Equip something on the head, chest, shoulders, hands, legs, and feet first.

## When you add or change equipment

- When you change the Library, for example by adding, reconverting, renaming, or deleting equipment, rebuild the MOD with "Build MOD" and then run "Install to Grim Dawn…" again. Until you rebuild, a notice is shown on the "MOD" screen.
- Clicking "Rebuild MOD" on the "Library" screen opens the "MOD" screen and builds the MOD.
- If a warning appears on the "MOD" screen after a game update (patch), follow its instructions to rebuild and reinstall the MOD (see [The "MOD" screen](#mod)).

## Without a GPU (from a GLB file)

1. On the "Setup" screen, click "Do it later". The TRELLIS runtime and models are not downloaded.
2. Click "Prepare from the game" under "Reference meshes (required for conversion)" in "Settings" to prepare the reference meshes (see [The "Settings" screen](#settings)). If you convert without preparing them, you can also prepare them with "Prepare reference meshes" in the error window.
3. On the "Generate" screen, choose "GLB file (advanced)", specify a GLB file made with another tool, and click "Start generation". The app only converts the file without generating anything.
4. From here on, the steps are the same as step 4 and later above (since nothing is generated, "Regenerate with another seed" and "Use this candidate" on the "Candidates" tab do not apply).

## Next steps

- To remove the installed MOD and return to the state before the first install, use "Uninstall…" on the "MOD" screen (see [Returning the game to its state before the MOD](#mod-restore)).
- To exchange your equipment or MOD with other people, see [Sharing equipment and MODs](#share).
- If something does not work, see [Troubleshooting](#troubleshooting).

# Installing and updating the app {#install}

GD Skin Studio does not use an installer. You extract the distribution ZIP to a folder of your choice and start `GDSkinStudio.exe`. Nothing is registered in the registry or the Start menu.

## System requirements

- Windows 10 / 11 (64-bit)
- Grim Dawn (Steam version) and its expansion Ashes of Malmouth (the MOD template uses Ashes of Malmouth data)
- Free disk space: about 270 MB for the app and about 13 GB for the TRELLIS runtime and models (more is needed temporarily while downloading; for a new download, the setup asks for about 18 GB of free space; adding text → 3D takes about 6 GB more)
- An internet connection for the first-run setup (about 8.6 GB is downloaded)

### What 3D generation needs

To make 3D models from images or text, you also need the following.

- An NVIDIA GPU (12 GB or more of GPU memory, 16 GB or more recommended, compute capability 6.0 or higher)
- NVIDIA driver R570 or later (one that supports CUDA 12.8)
- A recent version of the Microsoft Visual C++ Redistributable (x64) (an old version is often installed together with Grim Dawn, and with the old version, the self-test after the download fails; if the environment check reports it as old or missing, install the latest version from `https://aka.ms/vc14/vc_redist.x64.exe`; administrator rights are required)

> The app has been tested on only one PC: a GeForce RTX 5090 Laptop GPU (24 GB) with Windows 11 (Japanese). It has not been tested on other GPUs (RTX 40 series or older, etc.), on Windows 10, or on Windows in languages other than Japanese.
> On a GPU with at least 12 GB but less than 16 GB of GPU memory, a warning is shown before the setup download starts, and you can choose whether to continue. The TRELLIS README requires 16 GB or more, and generation on GPUs of this size has not been tested. If generation fails for lack of memory, try "Low VRAM mode" in "Advanced settings" on the "Generate" screen (whether it works on 12 to 16 GB GPUs has not been confirmed).
> Even without a GPU, you can convert GLB files made with other tools and build and install MODs. However, converting a GLB with more than 25,000 vertices or with several materials or textures uses a component (for rebuilding the UVs) that relies on the Windows Visual C++ runtime, so the recent Visual C++ Redistributable above is required (what happens on a PC with only an old version has not been tested).

### What you do not need

- You do not need the .NET runtime, Python, Visual Studio, the CUDA Toolkit, or git (everything needed is either bundled or downloaded during the first-run setup).
- Administrator rights are not needed either (except to install or reinstall the Visual C++ Redistributable).
- The 3D preview uses the Microsoft Edge WebView2 Runtime. It is included with Windows 11. If it is missing, install it with the "Evergreen Bootstrapper" on `https://developer.microsoft.com/microsoft-edge/webview2/`.

## Downloading and checking the ZIP

Download `GDSkinStudio-<version>-win-x64.zip` from the download page (GitHub Releases).

(Recommended) Check that the downloaded ZIP is not damaged or tampered with by comparing the SHA-256 of the ZIP shown on the download page with the SHA-256 of your ZIP. Open PowerShell in the folder that contains the ZIP and run the following (adjust the file name to your ZIP).

```
Get-FileHash .\GDSkinStudio-<version>-win-x64.zip -Algorithm SHA256
```

In Command Prompt, run the following.

```
certutil -hashfile GDSkinStudio-<version>-win-x64.zip SHA256
```

If the 64 characters shown match the value on the download page (uppercase and lowercase letters count as the same), the file is the same as the one distributed. If they do not match, do not use it; download it again. The SHA-256 of each file after extraction is in `MANIFEST.txt`.

## Installation steps

1. Extract the ZIP to a folder of your choice (for example `C:\Tools\GDSkinStudio`).
  - Avoid `C:\Program Files`, where writing is restricted.
  - A short folder path without full-width characters (such as Japanese) or non-English characters (such as é) is recommended.
  - The same applies to the data folder (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`). If your Windows user name contains full-width characters, the default data folder path contains them too. A workaround for the game's bundled tools not handling such paths is included, but it has not been tested on a real PC with a full-width user name. You can change the location of the data folder (see [Where the app keeps its data](#files)).
  - Do not run the app from inside the ZIP; always extract it.
2. Start `GDSkinStudio.exe` in the extracted folder. Only one instance of the app can run with the same data folder.
3. If "Windows protected your PC" appears, start the app with the steps below.
4. At the first start, the "Setup" screen opens. Go through it in order (see "First-run setup" below).

## If "Windows protected your PC" appears

This app's EXE is not code-signed. So at the first start, Microsoft Defender SmartScreen may show a blue window saying "Windows protected your PC" and "Microsoft Defender SmartScreen prevented an unrecognized app from starting" (only the "Don't run" button seems to be available). Check the SHA-256 of the ZIP first, then start the app as follows.

1. Click "More info" in the text of the window.
2. "App: GDSkinStudio.exe" and "Publisher: Unknown publisher" are shown; check the app name.
3. Click "Run anyway", which appears at the bottom.

In most cases, the window is not shown again for the same file.

- Before you extract the ZIP, you can right-click the ZIP → "Properties", check "Unblock" next to "Security: This file came from another computer and might be blocked to help protect this computer" at the bottom of the "General" tab, and click "OK"; then this window is not shown for the extracted files (do this only for a ZIP whose source you have checked).
- On a PC with Windows 11 "Smart App Control" turned on, the app may be blocked as an unsigned app, and no "Run anyway" button appears. In that case, this app cannot be used.
- Antivirus software may block (quarantine) `GDSkinStudio.exe` or `python\python.exe`. If you extracted them from a ZIP whose SHA-256 matches the one on the download page, consider whether to add the extracted folder to that software's exceptions (exclusions).

## First-run setup

At the first start, the "Setup" screen opens. Go through "1. Check environment", "2. Components and consent", "3. Download", "4. Reference meshes", and "5. Launch options" in order. Downloading the TRELLIS runtime and models used for 3D generation takes about 10 minutes to 1 hour, depending on your connection. For details, see [The "Setup" screen](#setup).

You can also put it off with "Do it later" (the screen then no longer opens automatically). Without the TRELLIS runtime and models, 3D generation from images and text cannot be used (converting GLB files and building MODs can). You can continue at any time from "Setup" on the left.

You can choose the display language, "日本語" (Japanese) or "English", in "Display language" in "Settings" on the left. After changing it, click "Save settings" and restart the app. The default is Japanese (see [The "Settings" screen](#settings)).

## Updating to a new version

To update, replace the app folder as a whole. The data folder (your equipment, MOD builds, install records, and settings) and the TRELLIS runtime are outside the app folder, so they are kept, and your equipment and downloaded models can be used as they are. The MOD installed in the game does not change either.

1. Close the app. If a task that cannot be interrupted, such as a MOD install or uninstall, is running, the app waits for it to finish and then closes.
2. Download the new ZIP and check its SHA-256 with the steps described above.
3. Delete the old extracted folder as a whole.
4. Extract the new ZIP and start `GDSkinStudio.exe`. SmartScreen may appear again at the first start of the new EXE.

After updating, do the following as needed.

- If the new version changes the version of a TRELLIS runtime component, that item shows "Different version" on the "Setup" screen. Going through the setup downloads it again (files already downloaded are verified and reused).
- If the license or notice information of what the setup downloads has changed, you are asked to accept it again (acceptance is recorded for the exact content shown).
- If the new version improves the conversion, "Reconvert" in the Library rebuilds saved equipment with the current version's conversion without redoing the 3D generation. After that, run "Build MOD" and "Install to Grim Dawn…" on the "MOD" screen again (see [The "Library" screen](#library)).

# The "Setup" screen {#setup}

This screen downloads the TRELLIS runtime and models used for 3D generation to this PC and prepares the game side (the reference meshes and the Steam launch option). The screen title is "First-run setup". It opens automatically at the first start and keeps opening at every start until you click "Finish and start generating" or "Do it later". You can open it and redo the setup at any time with "Setup" on the left.

The list of steps is on the left: the current step is highlighted, and finished steps get a check mark. The right side shows the current step.

- **"Next"**: Goes to the next step. In "2. Components and consent", it goes to "3. Download" without starting the download. In "3. Download", it is available only when the download is finished (or when the items needed for 3D generation from images are already installed).
- **"Back"**: Goes back to the previous step.
- **"Do it later"**: Stops the setup partway and goes to the "Generate" screen (details below).

> While the download or the reference mesh preparation is running, "Next", "Back", and "Do it later" are not available.

## 1. Check environment

Checks which features this PC can use. 3D generation requires an NVIDIA GPU (12 GB or more of GPU memory, 16 GB or more recommended, compute capability 6.0 or higher) and driver R570 or later. The mark at the start of each row means: a check mark for no problem, a warning sign for a caution, and an × for unavailable or not found.

- **"OS"**: The Windows name and build number.
- **"GPU"**: The NVIDIA GPU that was found and its driver version. If none is found, the row says that 3D generation is unavailable (GLB conversion and MOD builds are available), and the GPU rows below and the Visual C++ Redistributable row are not shown.
- **"GPU memory"**: With less than 12 GB, 3D generation cannot be used. With less than 16 GB, a caution says that generation may fail for lack of memory.
- **"GPU driver"**: R570 or later is required.
- **"GPU generation (compute capability)"**: 6.0 or higher is required. Generation on GPUs below 7.0 has not been tested.
- **"Visual C++ Redistributable"**: A caution appears if it is old or not installed. With an old version, the self-test after the download fails. Install the latest version (`https://aka.ms/vc14/vc_redist.x64.exe`; administrator rights are required).
- **"Free space"**: The free space on the destination drive, and the free space needed to download the required items (including a margin).
- **"Grim Dawn"**: The Grim Dawn install folder that was found.
- **"Expansion Ashes of Malmouth"**: Required to build the MOD.
- **"WebView2"**: The WebView2 Runtime used for the 3D preview. If it is not found, install it with the "Evergreen Bootstrapper" on Microsoft's download page (`https://developer.microsoft.com/microsoft-edge/webview2/`).
- **"Check again"**: Checks again. Click it after installing a driver or the Visual C++ Redistributable.

A summary appears below the list. On a PC that cannot use 3D generation, you can skip downloading the TRELLIS runtime and use only GLB file conversion and MOD builds. On a PC with at least 12 GB but less than 16 GB of GPU memory, you are asked whether to continue when you start the download. For a check that also covers other items, such as the data and temporary folder paths and the game's bundled tools, see "Environment check" on the "Settings" screen.

> The app has been tested only with a GeForce RTX 5090 Laptop GPU (Windows 11). Generation on GPUs with 12 to 16 GB of GPU memory has not been tested (the TRELLIS README requires 16 GB or more). If generation fails for lack of memory, try "Low VRAM mode" under "Generation settings" → "Advanced settings" on the ["Generate" screen](#generate) (whether it helps on 12 to 16 GB GPUs has not been confirmed).

## 2. Components and consent

The list of what is downloaded. These items are not in the distribution ZIP; they are downloaded directly to this PC from their sources. Read the full license text and notices of each item before agreeing. Your images and text are never sent.

### Destination

- **"Destination (TRELLIS runtime and models)"**: The folder for the TRELLIS runtime and models. Items are created directly in the folder you enter, so specify a dedicated empty folder. A path you type in the field is used as is. When you leave the field after typing, the status of that location is loaded again.
- **"Browse…"**: Chooses a folder. A `trellis-runtime` folder is created inside the folder you choose and used as the destination (if you choose a `trellis-runtime` folder itself, that folder is used as the destination).
- **"Reset to default"**: Returns to the default destination, which is inside the data folder (`%LOCALAPPDATA%\GrimDawnSkinMOD\trellis-runtime`). On a PC where that location cannot be used, for example because your user folder name contains full-width characters, it is `%SystemDrive%\GDSkinStudio\trellis-runtime` (`C:\GDSkinStudio\trellis-runtime` on most PCs).
- **"Use an existing TRELLIS runtime…"**: If you have a TRELLIS runtime made with `setup_trellis_env.ps1` or similar, choose its folder (the folder that contains `venv`) to use it and skip the download. Choosing it does not change its contents. When you click "Start download", missing items are added, and items of a different version are replaced with the pinned version after you confirm (including recreating the virtual environment). An existing runtime is not covered by "TRELLIS runtime management (re-verify, reinstall, remove)" on the "Settings" screen.

The destination must follow these rules. If it cannot be used, the reason appears below the field, and "Start download" is not available.

- Use a path of 80 characters or fewer that contains no characters other than ASCII letters, digits, and symbols (such as Japanese or full-width characters).
- The root of a drive, the Windows and Program Files folders (and folders inside them), the Grim Dawn folder (folders inside it and folders that contain it), the app's data folder itself (and folders that contain it), and folders inside the app folder cannot be used.
- Free space needed for a new download: about 18 GB for the required items (about 13 GB after the download), plus about 6 GB more for text → 3D.

After the download, the app uses the TRELLIS runtime in that destination. The destination is separate from the app folder, so it is kept when the app is updated.

### Required and optional items

Each item to download is shown on its own row.

- **"Required items (image → 3D)"**: Needed for 3D generation from images. They cannot be cleared.
  - uv (tool that creates the Python environment)
  - Python and the virtual environment (placed only inside the TRELLIS runtime; PATH and the registry are not changed)
  - PyTorch, torchvision, and xformers (used for the 3D generation computations)
  - Packages used by TRELLIS
  - TRELLIS program and FlexiCubes
  - TRELLIS-image-large (image → 3D model)
  - DINOv2 (model that extracts image features)
  - u2net (automatic background removal model)
- **"Optional items (text → 3D)"**: Needed only to create 3D models from text. They are not selected by default. TRELLIS-text-xlarge (text → 3D model) and CLIP (model that reads text) go together: selecting either one selects both, and clearing either one clears both. You can also add them later by running the setup again.

Each row has the following.

- Check box: Selects whether to download the item. Required items cannot be cleared.
- Name and description: The item name and a description of what it is used for.
- **"Required" or "Optional"**: The kind of item.
- **"Includes GPL", "Includes files under a non-commercial license", "Has a notice (acceptance required)"**: Marks on items that need attention. The notes are shown below the item.
- **"License:" and "Source:"**: The license name, and the name and URL of the source.
- Download and disk sizes: The amount to download, and the size on disk after the download.
- State: The current state in the destination ("Installed", "Not installed", "Partial", or "Different version").
- **"Full license"**: Shows the full license text and the notices in a separate window.
- **"Open source page"**: Opens the source page in your browser.

!> kaolin, one of the packages used by TRELLIS, includes kaolin/non_commercial, which is under the NVIDIA Source Code License and may be used only non-commercially (for research or evaluation purposes). Use it only within the terms of this license. The packages also include plyfile, which is under GPL-3.0 or later.

> The CLIP model card describes the model as a research output and states that any deployed use, whether commercial or not, is currently out of scope.

### Consent and download

- Totals: The number of items to download, the amount to download, and the free space needed (including a margin). If all selected items are already installed, "Start download" verifies the files (only what fails verification is downloaded again).
- Free space: The free space on the destination drive. If it is not enough, a message asks you to choose another drive or free up space, and "Start download" is not available.
- **"Show and accept the CLIP notice…"**: Appears when CLIP is selected and you have not accepted the current notice yet. Read the notice (in the display language), check "I have read the above and agree", and click "Agree". After you accept, "Accepted" is shown.
- **"I have read the licenses (full text) and notices of the items above and agree (includes plyfile under GPL-3.0 or later)"**: Your agreement to the licenses and notices of the selected items. The download cannot start unless this is checked.
- **"Start download"**: Moves to "3. Download", downloads the selected items from their sources, and verifies them with hashes (SHA-256). Models and program sources that are already installed are verified and reused; for PyTorch and the packages, only the versions are checked.

"Start download" is available when the destination has no problems, the agreement is checked (and, if CLIP is selected, its notice is accepted), there is enough free space, and the PC has a GPU that can be used for 3D generation. On a PC without such a GPU, "This PC has no supported GPU, so the 3D generation components cannot be downloaded." appears next to the button.

> Acceptance is recorded for the exact content shown. If the license or notice text changes, you are asked to accept again (files already downloaded are verified and reused).

### Confirmations before the download

When you click "Start download", the following confirmations may appear.

- **"Before downloading"**: Appears when the GPU memory is below the recommendation (16 GB or more), or when the Visual C++ Redistributable is old or not installed. Choose "Continue download" or "Cancel". Even if you stop, you can redo the setup at any time later.
- **"Confirm the destination"**: Appears when the destination contains files or folders that the app did not create. If you continue, `venv`, `models`, `downloads`, and so on are created directly in that folder. A folder that had contents before the download cannot be removed or repaired from "TRELLIS runtime management (re-verify, reinstall, remove)" on the "Settings" screen. Normally, click "Cancel" and choose a dedicated empty folder as the destination (to continue, click "Download into this folder").
- **"Confirm changes to the existing runtime"**: Appears when the destination was not created by the app's setup and something must be deleted or replaced to bring items of a different version to the pinned version (recreating the Python virtual environment, replacing a source folder, or replacing package versions). This cannot be undone. Check "I confirm that these may be deleted or replaced" and click "Replace and download" to start. If another app or other work uses that environment, click "Cancel" and choose an empty folder as the destination.

## 3. Download

Shows the progress of the download. It takes about 10 minutes to 1 hour, depending on your connection.

- Overall progress: The overall percentage, the elapsed time, and the current task ("Download and install", "Verify", or "Self-test", with the item name and file).
- Item list: For each selected item, its state ("Waiting", "Downloading", "Verifying", "Downloaded", "Verified and reused", "Failed", or "Cancelled"), the amount downloaded, and the speed.
- **"Details"**: The latest 8 lines of the download log.
- **"Cancel"**: Cancels the download. Partial files are kept, so the next download continues where it left off.
- **"Retry (continue)"**: Appears after the download fails or is canceled. Files already downloaded are verified and reused, and partially downloaded files are resumed. If it keeps failing, check the cause on the ["Log" screen](#log).

If the connection drops, the download is retried automatically and continues where it stopped. Downloaded files are verified with hashes (SHA-256) and downloaded again if they do not match. When the TRELLIS runtime is downloaded or changed, a self-test (a small computation on the GPU) runs at the end. When everything is finished, "The download and checks are finished" appears; click "Next" to continue.

> If you close the app during the download, the download is canceled, but the next time you click "Start download" in the setup, it continues where it left off.
> During the download, 3D generation from images or text and "Check background removal" cannot be started (GLB file conversion can be used). Conversely, the download cannot start while 3D generation, conversion (including reconversion), or a background removal check is running.

## 4. Reference meshes

Conversion needs the body meshes of the game's characters (reference meshes). Game files cannot be distributed, so they are extracted from your Grim Dawn and saved in the data folder (the game folder is not changed).

- Grim Dawn row: The Grim Dawn install folder that was found. If it is not found, "The Grim Dawn install folder was not found. Specify its location in Settings." appears.
- **"Set the Grim Dawn location…"**: Appears when Grim Dawn is not found. It opens the "Settings" screen: specify the folder in "Grim Dawn install folder", click "Save settings", and return to this step with "Setup" on the left.
- Status row: "Ready.", or guidance when the meshes are not available yet, are corrupted (extract them again with "Prepare from the game"), or cannot be opened.
- **"Prepare from the game"**: Extracts the game's official body meshes (6 files) by reading the Grim Dawn .arc files directly (read-only) and saves them in the data folder. The game files are not changed or copied. Progress is shown while it runs.
- **"Cancel"**: Cancels the preparation.

If the status row says that the meshes exist but cannot be opened, close or pause antivirus, backup, or sync software, or check your permission to read the files, then try again ("Prepare from the game" does not fix this). You can also prepare the reference meshes with "Prepare from the game" on the "Settings" screen.

## 5. Launch options

The MOD made by this app uses the `/basemods` method. Add `/basemods` to the Steam launch options to load the MOD in the main campaign. This app does not change the Steam launch options automatically (Steam could overwrite the setting while it is running).

- Check result: Shows whether `/basemods` is set in the launch options of the user currently logged in to Steam (or, if that cannot be determined, the user who last logged in). The app only reads the setting and does not change it.
- **"Check again"**: Checks the setting again. If you have just set it while Steam is running, the setting is saved to a file when you exit Steam, and then it can be checked.

The "Steps" area shows the following steps.

1. In the Steam library, right-click Grim Dawn and choose "Properties".
2. Under "General" → "Launch Options", enter `/basemods` (if there are other options, add it at the end, separated by a space).
3. Start Grim Dawn from Steam (install the MOD you build on the "MOD" screen).

> Only one `mods\database.arz` is read, so this MOD cannot be used together with other main-campaign MODs. For installing the MOD and using it in the game, see [Installing a MOD and using it in the game](#mod-install).

## Done

A summary shows whether 3D generation from images, 3D generation from text, the reference meshes, and the launch option `/basemods` are available (or ready).

- **"Finish and start generating"**: Records the setup as completed and goes to the "Generate" screen. From then on, this screen no longer opens automatically at startup.

## Doing it later and redoing it

- **"Do it later"**: In the confirmation ("Do the setup later?"), click "Do it later" again to go to the "Generate" screen without finishing the setup. From then on, this screen no longer opens automatically at startup. Without the TRELLIS runtime and models, 3D generation from images and text is unavailable, but GLB file conversion and MOD builds are available. You can continue at any time with "Setup" on the left.
- **"Redo setup"**: A button on the "Settings" screen. It starts the setup again from step 1 (the agreement check box is cleared too). It also clears the record that the setup was finished or postponed, so this screen opens at every start until you click "Finish and start generating" or "Do it later" again. When you redo the setup, installed items are verified and reused, and only missing items are downloaded. To add the text → 3D items later, redo the setup and select them.

### What you can skip

- The text → 3D items (TRELLIS-text-xlarge and CLIP) are optional.
- If you do not use 3D generation (for example, on a PC without a usable GPU), you do not need to download the TRELLIS runtime and models; click "Do it later". Without the TRELLIS runtime, "Next" does not take you past "3. Download", so prepare the reference meshes with "Prepare from the game" on the "Settings" screen. To see the steps for setting the launch option `/basemods`, click "How to set /basemods" on the "MOD" screen. Some GLB files need a recent Visual C++ Redistributable for the conversion (see [Installing and updating the app](#install)).
- The reference meshes are needed before any conversion, including GLB file conversion. The launch option `/basemods` is needed before you use the MOD in the game.

You can check the installed items and their licenses in "Setup (installed items and licenses)" on the "Settings" screen. To re-verify, reinstall, or remove the downloaded TRELLIS runtime, use "TRELLIS runtime management (re-verify, reinstall, remove)" on the "Settings" screen (only destinations created by the app's setup are covered). For details, see [The "Settings" screen](#settings).

## If something goes wrong

- If the download fails, click "Retry (continue)". The download fails if connections to the sources (such as `github.com`, `huggingface.co`, `download.pytorch.org`, and `pypi.org`) are blocked, for example on a company or school network.
- If "The Visual C++ Redistributable is required" appears, install the latest version, then click "Retry (continue)". Files already downloaded are reused.
- If "Not enough free space" appears, choose a destination on a drive with more free space. If the destination path is reported as too long, use a short path of 80 characters or fewer.

For other problems, see [Troubleshooting](#troubleshooting).

# The "Generate" screen {#generate}

Click "Generate" on the left to open this screen (its title is "Generate a 3D model"). It creates a 3D model from images or text, converts it for Grim Dawn's male and female characters, and saves it as equipment. Your images and text are never sent outside this PC.

The left side of the screen is for the input, the generation settings, running the generation, and saving; the "Preview" on the right shows the result.

## Basic steps

1. Under "Input", choose "Image", "Text", or "GLB file (advanced)", and specify the images, the text, or the GLB file.
2. If needed, open "Generation settings" and change the settings (the defaults are usually fine).
3. Click "Start generation". The app runs the 3D generation and the conversion for male and female characters (about 1 to 2 minutes).
4. Check the result in the "Preview" on the right. If the model faces backwards, use "Rotate 180° and reconvert". To use another body, use "Use this candidate" on the "Candidates" tab or "Regenerate with another seed".
5. Enter a name under "Name and save as equipment" and click "Save as equipment" to add it to the Library.
6. To use it in the game, build and install the MOD on the "MOD" screen (see [Installing a MOD and using it in the game](#mod-install)).

## Input

Under "Input", choose what to create the 3D model from.

- **"Image"**: Creates it from images of one character showing the whole body (1 to 4 images).
- **"Text"**: Creates it from English text. You need to download the optional text → 3D components (TRELLIS-text-xlarge and CLIP) in the setup (see [The "Setup" screen](#setup)) and accept the CLIP notice.
- **"GLB file (advanced)"**: Only converts a GLB file made with another tool, without 3D generation.

### Creating from images

Supported formats are PNG, JPEG, WebP, and BMP. A PNG with a transparent background works best, and a standing pose with the arms slightly away from the body converts best. HEIC images, such as iPhone photos, cannot be used; convert them to JPEG or PNG before selecting them. Each image can be up to 40 MB and 64 to 16,384 pixels per side (up to 160 million pixels in total).

- **"Number of images"**: Choose from "1 (front only)" up to 4 images. One image field is shown for each image. With two or more images, you can also use back and side views.
- **"Image name"**: The field to the right of the number. The defaults are "Front", "Back", "Left side", and "Right side". It does not affect the result and is kept in the record (up to 32 characters).
- **"Image file path"**: The field to the right of that. Select an image with "Browse…", or type or paste the path (surrounding `"` marks are removed automatically).
- Image box: Shows the selected image (transparent areas show a checkerboard pattern). The image's size, format, and whether it has transparency are shown below it.
- **"Original image"** / **"After background removal"**: Appear after "Check background removal" or after generation. They switch between the original image and the image used for generation (with the background removed and cropped).
- **"Check background removal"**: Creates and shows the image used for generation (background removed and cropped to 518×518) before generating (requires the TRELLIS runtime). Use it to check that no part of the character is missing and that no background remains. An image with transparent areas is only cropped, without background removal.
- **"Advanced settings for this image"**: Shown only for an image with transparent areas. Turning on "Force background removal (ignore the transparent areas and remove the background automatically before generation)" fills the transparent areas with white and then removes the background. It is meant for images that are almost entirely opaque (screenshots with only transparent corners, PNGs with shadows or borders, etc.) and requires the background removal model (u2net). Normally leave it off. It turns off again when you change the image.

Depending on the image, the image field shows the following notices.

- Images without a transparent background: If the background removal model (u2net) is available, the background is removed automatically before generation (processed on this PC). Without the model, such an image cannot be used as is; download the background removal model in the setup, or use a PNG with a transparent background.
- Images that are almost entirely opaque: Even with some transparent areas, an image that is almost entirely opaque, such as a screenshot with only transparent corners, does not get its background removed, and the background becomes part of the 3D model. Force background removal in "Advanced settings for this image", or use a PNG with a transparent background.
- Subject notes: When the subject touches the edge of the image or is small, or when there are several separate objects, a note starting with "Notes about the subject (generation is not stopped):" appears (for an image with a background, it appears after "Check background removal"). Check it with the "After background removal" view, for example.

> When you make a transparent PNG yourself, make the background fully transparent and the character opaque. Semi-transparent parts (light, hair, shadows, etc.) disappear or become dark.

> Generating from several images uses a training-free method (the image directions are not used), and the result is not always better than with one image. Use the same character, pose, and outfit in all images, and do not mix in images of other objects (no warning is shown if a mixed body is produced). Side views help with depth. The same image cannot be specified in two fields.

> For 3D generation, use only images that you have the right to use. Publishing or distributing models made from images of other people's works or characters may infringe the rights of the rights holders.

### Creating from text

In the text field, describe the character you want in English (example: a knight in full plate armor with a long red cape). A no-weapons instruction is added automatically (not when "Weapons" in the generation settings is turned on). Without colors, the body tends to come out white or gray, so also write the colors of the hair, clothes, shoes, and so on in English (example: long black hair, a red ribbon, a navy blue skirt). The text can be up to 1,000 characters.

- **"Template"**: English sentences that include the conditions for a figure that converts well (full body, one character, front view, standing with the arms slightly away from the body). Select one and click "Insert into text": "Prepend the conditions only (full body, front view, standing)" adds the conditions before your current text, and the examples (four of them, such as "Example: knight with a cape") replace the text with the example (a confirmation appears if you have already entered text). Pose instructions do not always take effect (the candidate selection compensates).
- Text length: Below the field, a line starting with "Length read by the generation AI:" shows an estimate of the token count of the text. The generation AI reads up to 77 tokens, including the two start and end markers (roughly 50 to 60 English words); anything beyond that is not used (the no-weapons instruction is kept). When the text is too long, a warning shows the part that is not used. Until you download CLIP and accept its notice, the count is an estimate.
- Text warnings: If the text contains many non-English characters, words with non-English characters, or full-width letters, digits, or symbols (such as the full-width forms of a, 1, parentheses, and the tilde), a warning asks you to fix them (generation is not stopped).
- **"Read the notice…"**: Appears while you have not accepted the CLIP notice. Click it to open "Notice for CLIP (generation from text)", read it, check "I have read the above and agree", and click "Agree". You cannot click "Start generation" until you accept (generation from images works without accepting). It does not appear if you accepted in the setup. When the notice is updated, you need to accept it again.

### Converting a GLB file

Converts a GLB made with another tool (Y up, front +Z; the height is adjusted automatically) without 3D generation. No GPU is needed.

- **"GLB file path"**: Enter the path of the GLB file, or select it with "Browse…".

For a GLB that references texture images or `.bin` as external files, only files referenced by a relative path from the GLB's folder are loaded (if a GLB references a file with any other kind of path, such as an absolute path, another drive, or a network location, the app stops without reading that file). Of the generation settings, GLB input uses only "Target vertex count after conversion" and "MOD textures" in the advanced settings.

## Generation settings

Click the "Generation settings" heading to open it. The defaults are usually fine. When you click "Start generation", the current generation settings (including the advanced settings) are saved as the defaults for next time.

- **"Generation AI"**: The AI used for 3D generation (TRELLIS is the only choice).
- **"Model"**: The name of the model used (microsoft/TRELLIS-image-large for images and microsoft/TRELLIS-text-xlarge for text; "(No generation. Conversion only)" for GLB input).
- **"Random seed"**: The seed of the random numbers used for generation (0 to 2,147,483,647; default 1). With a different seed, you can make a different result from the same input.
- **"Pose selection"**: While "Pick the body closest to an A-pose from the candidates" is on (the default), the app makes as many candidates as the number in the field below ("bodies (number of candidates)"; 1 to 8, default 4), using consecutive seeds starting from the "Random seed", and picks the body whose arm angle is closest to the pose of the game's body. Candidates with a weapon-like object and candidates whose arm angle cannot be measured are excluded. If all are excluded, one more round of candidates is made with the following seeds. If no body meets the conditions even then, the closest body is used and this is shown under "Points to check". More candidates take longer. When off, only one body is made with the given seed.
- **"Weapons"**: Turning on "Also allow bodies holding weapons (normally off)" stops adding the no-weapons instruction to the text and stops excluding candidates with weapon-like objects.
- **"Detail"**: The detail of the 3D model (GLB) made by the generation AI. Choose "Standard (about 23,000 vertices)" (the default) or "Light (about 13,000 vertices)".
- **"Texture resolution"**: The size (pixels per side) of the texture made by the generation AI. Choose 512, 1024 (the default), or 2048.
- **"Target vertex count after conversion"**: When the converted mesh has more vertices than this, it is reduced to about this number (2,000 to 65,535; default 25,000).
- **"Multi-image mode"**: Used only with two or more images. Choose "Use the images in turn (stochastic)" (the default) or "Average the predictions of all images (multidiffusion)". In the evaluation, the quality was about the same; the former took almost the same time as one image, and the latter took a few seconds longer for each added image.

### Advanced settings

These appear when you open "Advanced settings" inside "Generation settings". Normally you do not need to change them (the defaults are Standard for "Sampling", 2048 for "MOD textures", and off for the rest). When you change any setting other than "Appearance remake" from its default, the heading becomes "Advanced settings (changed from defaults)". Settings changed from the defaults are recorded in the draft and the equipment record.

- **"Sampling"**: The number of computation steps the generation AI uses to create the shape and appearance, and how strongly it follows the input (guidance strength). Choose "Standard (the model's default, 25 steps)" (the default) or "Fast (12 steps)". "Fast (12 steps)" uses the settings of the official TRELLIS demo for image input (for text input, it only reduces the number of steps to 12) and takes about half the time per candidate, but the shape and detail change even with the same seed. Not used for GLB input.
- **"Low VRAM mode"**: Turning on "Unload models that are no longer needed from GPU memory while building the mesh" lowers peak GPU memory use by about 3.5 to 4.5 GB (on a 24 GB GPU, it was measured to drop from about 14 GB to about 10 GB; whether it works on 12 to 16 GB GPUs has not been confirmed). The computation is the same; each candidate takes about 2 seconds longer. Try it when the GPU has less than 16 GB of memory or when generation fails for lack of memory.
- **"Block network access"**: Turning on "Block all outgoing connections during generation (strict offline)" refuses all outgoing connections from the generation process (Python) and writes the attempted destinations to the log (connections that native code opens by itself are not covered). Generation does not use the network anyway; this setting makes sure of it. If a required file is missing, generation stops with an error instead of downloading it.
- **"MOD textures"**: The maximum size (pixels per side) of the textures made by the conversion. Choose "2048 (default)", 1024, or 512. At 1024, the textures in the MOD are about a quarter of the size (the game's official equipment uses 1024 or less). Making it larger than "Texture resolution" does not add detail. How equipment made with 1024 or 512 looks in the game has not been confirmed.
- **"Keep candidates"**: Turning on "Keep the candidates (stored in the draft only)" keeps the generation result of each candidate in the draft, so that "Use this candidate" on the "Candidates" tab and "Rebuild GLB" below the preview work without redoing the 3D generation. Uses about 45 MB per body (about 180 MB for 4 candidates; twice that if the candidate selection goes to a second round). It is not included in equipment or share files. The results are removed when the draft is saved or discarded (they are kept when "Keep intermediate generation files (drafts) after saving" is on in Settings, and for drafts whose conversion failed). Not available for GLB input or appearance remakes.
- **"Background removal"**: Turning on "Remove the background in the app before generation (pass the transparent image to the generation AI)" uses an image whose background was removed by the same process as "Check background removal", and lets you choose the post-processing options "Smooth the mask (post_process_mask)", "Remove background color left at the edges (decontaminate)", and alpha matting (alpha_matting) to refine the edges. Alpha matting takes several to tens of seconds on the CPU, and when it is on, "Remove background color left at the edges (decontaminate)" is not used. The effect depends on the image, so compare the results with "Check background removal" before using it (the effect has not been evaluated yet). When off (the default), the background is removed inside the generation AI. Available only for image input; images with transparent areas are not affected (unless background removal is forced).
- **"Appearance remake"**: Checking it shows a "Remake appearance from text…" button in the Library (experimental). This feature remakes only the colors, materials, and decorations from English text while keeping the shape (pose) of the generation result of saved equipment, and saves the result as new equipment (see the part about opening this screen from the Library below). It requires the optional text → 3D components and acceptance of the CLIP notice. It has been tested with only 1 sample and 2 texts; the colors may not match the text, and small details of the shape may change. This setting is saved as soon as you change the check box.

## Running the generation

- **"Start generation"**: Starts the 3D generation and the conversion. It can be clicked once the input is ready (for images, a file in every image field shown; for text, some text and an accepted CLIP notice; for GLB, an existing file).
- **"Stop"**: Stops the running process (generation, reconversion, rebuilding from a candidate, etc.). When you stop a generation, its result is not kept, and the result shown before stays as it was.
- **"Regenerate with another seed"**: Available while a result is shown and the input is ready. Makes it again from the same input with the seed after the ones tried in the previous generation (including the candidates). The "Random seed" field also changes to that seed.
- Progress: A progress bar, the current stage (while candidates are being made, the candidate number, such as (1/4), follows "Generating the 3D model"), the elapsed seconds, and a result message are shown.
- **"Back to previous result"**: Appears when generation fails while an unsaved result was shown. It shows the result from before the failure again.

The 3D generation and conversion take about 1 to 2 minutes. If you switch to another screen while it runs, the process continues. If something needed for 3D generation (such as the TRELLIS runtime or a model) is missing, the reason and what to do are shown above "Start generation". The conversion uses `TextureCompiler.exe` in the Grim Dawn folder and the reference meshes prepared in the setup.

When generation fails, an error window appears. If the process had progressed partway, the failed result is shown (it cannot be saved). See the error window and the "Log" screen for details (see [Troubleshooting](#troubleshooting)). Failed results can be deleted with "Discard" or from "Data usage and cleanup" in Settings.

> Warnings during generation (low free GPU memory, Grim Dawn running, etc.) appear on the "Log" screen. 3D generation cannot be started while the setup is downloading or while the TRELLIS runtime is being re-verified, reinstalled, or removed.

## Preview

The right side shows the current result. Next to the "Preview" heading, a summary appears with the seed, vertex count, arm angle, model used, and so on.

### The "3D preview" tab

- **"Generated"** / **"Male (converted)"** / **"Female (converted)"**: Switch the model shown. "Generated" is the 3D model made by the generation AI before conversion (for GLB input, the GLB you loaded); the other two are the results converted for the game.
- **"Front"** / **"Back"** / **"Left side"** / **"Right side"** / **"Angled"**: View the model from that direction.
- **"Texture"**: Clear it to show the model in gray without the texture.
- **"Wireframe"**: Shows the mesh as lines.
- **"Front marker"**: Appears while a 3D model is shown. Shows an arrow on the floor and the label "Front (+Z)" in the in-game front direction (+Z). Depending on the window width, the check box appears at the right end of the model selection row or to the right of "Wireframe".
- Mouse: Left-drag to rotate, use the wheel to zoom, and right-drag to pan. The vertex and triangle counts and the height are shown in the view.

If the 3D view cannot be shown (the WebView2 Runtime is missing, WebGL is not available, etc.), the reason and what to do appear in the view. You can also check the conversion result on the "Conversion check image" tab. If the view stops, click a model button above to load it again.

### The "Conversion check image" tab

After conversion, this tab shows the fitting and weight check image.

### The "Candidates" tab

When you generate with "Pose selection" on, an image with front views of the candidates appears. The text in the image is in English: CHOSEN marks the candidate chosen by the automatic selection, and excluded marks the excluded candidates. Below it, there is a row for each candidate.

- Row heading: The candidate's seed and, where applicable, the marks "auto-selected" and "current body".
- Row details: The arm angle ("Arm angle cannot be measured" if it cannot be measured), the color ("Has color" or "Almost no color (white, gray, black)"), "Possible weapon", the reason for exclusion by the automatic selection, and whether the generation result is saved (such as "Generation result saved"). An empty candidate shows "Empty result (cannot be selected)".
- **"Use this candidate"**: Shown in the rows of candidates that are not the current body and not empty. For a candidate whose generation result is saved, the GLB is built from that body and converted without redoing the 3D generation ("Detail" and "Texture resolution" stay as they were at generation). For a candidate that is not saved, after a confirmation, the 3D generation is redone with that seed and the result is converted (no candidate selection; it takes about 1 minute). Even with the same seed, the body may not be exactly the same as in the candidate image. To use the chosen body as is, turn on "Keep candidates" before generating.
- If you stopped during the conversion or the conversion failed, "Use this candidate" also appears in the row of the current body and redoes only the conversion.

When you choose another body, it is converted without rotation. If the previous body was converted with a 180° rotation, check the front direction again.

### Below the preview

- **"Rotate 180° and reconvert"**: Use this when the model faces backwards (when you see the back while viewing "Male (converted)" or "Female (converted)" from "Front"). The generated 3D model is rotated 180° around the vertical axis and then converted again into the male and female equipment (the 3D generation is not repeated). After rotating, the button changes to "Restore the original direction and reconvert". "Generated" is still shown as it was before rotation. The candidate selection (arm angle and weapon checks) was done in the direction before rotation.
- **"Rebuild GLB"**: Appears when the result was generated with "Keep candidates" on and the generation result of the current body is saved. Rebuilds the GLB from the saved generation result with the current "Detail" and "Texture resolution" in the generation settings and converts it (no 3D generation). Use it to change the detail while keeping the same body.
- **"Points to check"**: Lists points in the result that you should check. They do not prevent saving. The main points and what to do are as follows.
  - Parts that stretch a lot, stick out below the feet, or are large and away from the body: Check how they look in "Male (converted)", "Female (converted)", and the game. If they are broken, regenerate with another seed, or use an image of a standing pose with the arms and legs slightly apart.
  - A weapon-like object, or a weak pose fit: Regenerate with another seed, or choose another candidate on the "Candidates" tab.
  - A body with no color: Choose a candidate marked "Has color" on the "Candidates" tab, or write the colors in English in the text and generate again.
  - Text that was too long: The end of the text was not used for generation. If needed, shorten the text and generate again.
  - Notes about the input image (the background was not removed, the subject is cut off at the edge, etc.): Check in the preview, and if needed, change the image or the settings and generate again.

> A result whose conversion was stopped partway cannot be saved. Convert it again first ("Rotate 180° and reconvert" or "Restore the original direction and reconvert", or "Use this candidate" on the "Candidates" tab).

## Name and save as equipment

This appears while a result is shown.

- **"In-game name (English)"**: Required. The name shown in the game; plain ASCII letters, digits, and symbols are recommended (the game's fonts in English only have ASCII characters, so Japanese text or characters such as é and “ ” will probably not be shown). Up to 64 characters; `=` `^` `{` `}` `\` and line breaks cannot be used. Items with the same name are still saved as separate equipment. If the name has characters that will probably not be shown, a warning appears after saving.
- **"Japanese name (optional)"**: Used in the Library and for the game in Japanese (`Text_JA.arc`; the English name is used instead when this use is turned off on the "Settings" screen). As with the English name, it can be up to 64 characters, and the same symbols and line breaks cannot be used.
- **"Save the input image with the equipment (used for regeneration)"**: Shown only for image input. When on, the input images are saved with the equipment and used by "Regenerate" in the Library. Its initial value follows "Also save the input image when saving equipment (used for regeneration)" on the "Settings" screen.
- **"Save as equipment"**: Saves the result as equipment and switches to the "Library" screen. When regenerating, the button becomes "Replace the equipment with this result" and replaces the same equipment (its ID stays the same; if the English name is left empty, the name does not change).
- **"Discard"**: Deletes the unsaved result shown (including a failed result) after a confirmation. If there is a result from before a failed generation, it is shown again.

To use the saved equipment in the game, build and install the MOD on the "MOD" screen (see [Installing a MOD and using it in the game](#mod-install)).

## When opened from the Library

- "Regenerate" in the Library: Loads the equipment's input (images or text) and generation settings and opens this screen. A bar starting with "Regenerating:" appears at the top. The "Random seed" is set to the seed after the ones tried in the previous generation. Click "Start generation" to make it again; saving with "Replace the equipment with this result" replaces the same equipment. "Sampling", "MOD textures", "Background removal", and the per-image forced background removal return to the values in the equipment record ("Low VRAM mode" and "Block network access" keep their current settings). For equipment saved without its input images, select the images again.
- **"Create as new"**: The button on the regeneration bar. It cancels the replacement; saving after that creates new equipment.
- If the result shown was not made by regenerating that equipment, a confirmation appears before replacing. After replacing, the original model and generation record cannot be restored.
- "Remake appearance from text…" in the Library (shown when "Appearance remake" is turned on in the advanced settings): A bar starting with "Appearance remake: uses the shape of" appears at the top, and the input is limited to "Text". The shape and pose stay as in the original equipment's generation result, and only the appearance is remade from the English text. No candidate selection and no candidate saving are done. Saving creates new equipment; the original equipment does not change. "Regenerate" for equipment with a remade appearance uses the original equipment's shape (if the original equipment is not in the Library or "Appearance remake" is off, it is remade from the text only, so the shape also changes).
- **"Cancel"**: The button on the appearance remake bar. It stops the appearance remake.
- "Create new" in the Library: Opens this screen with the replacement and the appearance remake cleared.

## Results before saving (drafts)

Until you save it, a generation result stays as a draft in the `work` folder of the data folder (see [Where the app keeps its data](#files)).

- Even if you close the app, the previous unsaved result is shown the next time you start it ("Showing the previous generation result (not saved yet)." appears). For a regeneration result, the regeneration bar also comes back if the equipment to replace still exists.
- When you generate again (including "Regenerate with another seed"), the unsaved result shown is replaced by the new result and deleted. If generation fails, the previous result is kept, and "Back to previous result" shows it again.
- When you save, the draft is deleted. It is kept when "Keep intermediate generation files (drafts) after saving" is on in Settings (such a draft is not shown at the next startup).
- Failed drafts and drafts whose processing was cut off because the app (backend) exited (they become "interrupted" at the next startup) are not deleted automatically. You can delete them from "Data usage and cleanup" in Settings (see [The "Settings" screen](#settings)).

# The "Library" screen {#library}

This screen lists your saved equipment. On the "MOD" screen, the equipment here is combined into one MOD so that you can choose it at the Illusionist in the game. You also rename, regenerate, reconvert, delete, and restore equipment on this screen, and exchange it with other people.

The left side shows the list of equipment, and the right side shows the details and a preview of the selected equipment. The buttons are above the list; at their right end, the app shows the number of pieces of equipment and how many of them are installed.

## Buttons

- **"Create new"**: Switches to the "Generate" screen, ready to make new equipment (cancels regeneration and appearance remake, and clears the name fields; the input fields are left as they are).
- **"Regenerate"**: Loads the selected equipment's input (images, text, or GLB) and generation settings into the "Generate" screen. When you make it again and save it, the same equipment is replaced (its internal ID stays the same). See "Regenerate" below.
- **"Reconvert"**: Rebuilds the equipment from the saved generation result with the current conversion, without redoing the 3D generation. See "Reconvert" below.
- **"Delete"**: Removes the selected equipment from the Library. See "Deleting and restoring" below.
- **"Rebuild MOD"**: Switches to the "MOD" screen and builds the MOD from the equipment in the Library (the same as "Build MOD"). It does not install the MOD; to use it in the game, click "Install to Grim Dawn…" next.
- **"Refresh"**: Reloads the Library.
- **"Export…"**: Exports the selected equipment to one file (`.gdskin`). Hold down Ctrl or Shift while clicking to select several pieces of equipment (when two or more are selected, the number appears below the buttons). For the steps, see [Sharing equipment and MODs](#share).
- **"Import file…"**: Imports an equipment file (`.gdskin`) received from another person into the Library. For the steps, see [Sharing equipment and MODs](#share).

"Regenerate", "Reconvert", "Delete", and the fields on the right act on the one piece of equipment whose details are shown on the right. This is also the case when several pieces of equipment are selected.

> While equipment is being reconverted, exported, or imported, the progress is shown below the buttons. While a MOD is being built or installed, you cannot save names, delete, restore, or save consumable text. Try again after it finishes.

## The equipment list

Equipment is listed with the most recently created first. Each row shows the following.

- The small image on the left: part of the conversion check image (the converted result).
- The name: the English name. When there is a Japanese name, it is shown in parentheses.
- The internal ID, how it was made, and the creation time: the internal ID is a 12-character name starting with `eq` that each piece of equipment gets. How it was made is "Generated from image", "Generated from text", or "Converted from GLB" ("Prototype (development)" for development equipment).
- Conversion warnings: quality warnings found during the conversion. When there are none, "No warnings" is shown.
- The mark on the right: the MOD status.
  - "MOD not built": no MOD containing this equipment has been built yet (equipment whose name, consumable text, or 3D model was changed after the MOD was built, and equipment that was restored or imported, also get this status). To use it in the game, build and install the MOD.
  - "MOD built": a MOD containing this equipment has been built but not installed yet.
  - "MOD build failed": the MOD build failed.
  - "Installed": a MOD containing this equipment has been built and installed in the game.
- Below the mark: the conversion status ("Converted", "Waiting for conversion", "Conversion failed", or "No conversion needed").

When there is no equipment, a message asks you to make a 3D model on the "Generate" screen and save it. "Deleted equipment…" at the bottom right of the list is used to restore deleted equipment (see "Deleting and restoring" below).

## Details of the selected equipment (right side)

### "Edit names"

- **"In-game name (English)"**: The name shown in the game when it is set to English (this name is also used in the name files for the languages other than Japanese). Required, up to 64 characters. The characters = ^ { } \ and line breaks cannot be used. The game's fonts in English only have ASCII characters, so plain ASCII letters, digits, and symbols are recommended (Japanese text or characters such as é and “ ” will probably not be shown).
- **"Japanese name (optional)"**: Used in the Library and for the game in Japanese (`Text_JA.arc`). Up to 64 characters. If you save it blank, the Japanese name is removed. Without a Japanese name, the English name is also used for the game in Japanese.
- **"Save names"**: Saves the names in the fields. You cannot click it while the names are the same as the current ones. If the English name contains characters that probably will not be shown in the game, a warning appears after saving.
- **"Open folder"**: Opens the folder with this equipment's files (`library\equipment\<internal ID>` in the data folder) in File Explorer.

When you change the names, the equipment goes back to "MOD not built". To update the game, rebuild and install the MOD.

### Equipment information

Below the name fields, the app shows the following.

- The model and seed used for generation.
- The input: for equipment made from images, the number and names of the images (and the multi-image mode when there are several images). For equipment whose appearance was remade, the internal ID of the original equipment whose shape was used.
- For equipment that was rotated 180° before conversion: "Conversion: converted with 180° rotation (the generated model faced backwards)".
- The vertex counts of the male and female meshes.
- Whether the consumable's name and description are the default text or have been changed.

- **"Consumable text…"**: Changes the name and description of the consumable sold by the Quartermaster in the game (the item that adds the illusion to your collection when used). See "Consumable name and description" below.
- **"Remake appearance from text…"**: Shown only when "Appearance remake" is turned on under "Generation settings" → "Advanced settings" on the "Generate" screen. See "Remaking the appearance from text (experimental)" below.

### Preview

The preview of the selected equipment is shown at the bottom right. You can switch between "Generated" (only for equipment with a saved generation result), "Male (converted)", and "Female (converted)" and rotate them to check. It has the "3D preview", "Conversion check image", and "Candidates" tabs. It works the same as the preview on [the "Generate" screen](#generate). However, the "Candidates" tab shows only the candidate image. The list of candidates, "Use this candidate", and the buttons below the preview are only on the "Generate" screen (to fix the front direction here, use "Reconvert").

## Regenerate

1. Select the equipment in the list and click "Regenerate".
2. The app switches to the "Generate" screen and loads the equipment's input and generation settings. The seed is set to the one after the seeds tried in the previous generation.
3. Change the input or settings if needed, and click "Start generation".
4. Check the result and click "Replace the equipment with this result" to replace the 3D model of the same equipment (its internal ID stays the same). The equipment goes back to "MOD not built", so rebuild and install the MOD.

- For equipment without a saved input image, "No input image is saved with this equipment. Select the image again." appears. Select the image again before generating.
- The "Sampling", "MOD textures", and "Background removal" settings, and the forced background removal for each image, are set back to the values recorded with the equipment. "Low VRAM mode" and "Block network access" keep their current settings.
- To make new equipment instead of replacing it, click "Create as new" on the "Generate" screen. For details, see [the "Generate" screen](#generate).

## Reconvert

Rebuilds the male and female equipment from the saved generation result with the conversion of the current version, without redoing the 3D generation. Use it when an app update improves the conversion, or to fix equipment that looks backwards in the game. You cannot click it for equipment without a saved generation result (such as equipment received in an equipment package).

1. Select the equipment in the list and click "Reconvert".
2. In the confirmation window, check "What stays the same" and "What is renewed". To change the front direction, check the box (it is unchecked by default; if you leave it unchecked, the equipment is reconverted in its current direction).
  - "Rotate the front 180° and reconvert (when it looks backwards in the game or the preview)": rotates the generation result 180° before converting.
  - "Restore the original direction and reconvert (the direction before the 180° rotation)": for equipment that was converted with a 180° rotation, the box shows this text instead.
3. Click "Reconvert". This takes 1 to 2 minutes.
4. When it finishes, the conversion warnings after reconverting and those from the previous conversion are shown. Check the feet etc. in the "Male (converted)" and "Female (converted)" previews.
5. Click "Rebuild the MOD" to build the MOD on the "MOD" screen. Then install it with "Install to Grim Dawn…" to update the game.

- What stays the same: the internal ID, in-game records and tags, names, the generation record (input images or text, seed), and the generated 3D model. Because the in-game records do not change, illusions already obtained in the game should keep working as the same items (not confirmed in the game).
- What is renewed: the male and female meshes and textures, the previews, and the conversion warnings. The equipment goes back to "MOD not built".
- The MOD texture size is not taken from the current advanced generation settings; the equipment is converted at the same size as when it was made.
- If the conversion fails, the equipment does not change.

## Deleting and restoring

### Delete

1. Select the equipment in the list and click "Delete".
2. Click "Delete" in the confirmation window to remove the equipment from the Library.

- The equipment files are only moved to `library\trash` in the data folder, not deleted permanently.
- The next MOD build no longer includes the equipment.
- Equipment in the installed MOD stays in the game until you rebuild and install the MOD. What the game shows when a collected illusion disappears has not been confirmed.

### Restore deleted equipment

1. Click "Deleted equipment…" at the bottom right of the list.
2. In the "Deleted equipment" window, select the equipment to restore and click "Restore" (you can also double-click equipment that can be restored).
3. After it is back in the Library, rebuild and install the MOD.

- The list shows the check image, name, internal ID, how it was made, and when it was deleted.
- The equipment comes back with the same internal ID and in-game records, so illusions already obtained in the game should keep working as the same items (not confirmed in the game).
- Restored equipment has the "MOD not built" status.
- Equipment cannot be restored in cases such as the following. The reason is shown in the list, and "Restore" cannot be clicked.
  - Equipment with the same internal ID is in the Library (for example, equipment replaced by an equipment package).
  - Its record paths, tag names, or similar overlap with other equipment in the Library.
  - The equipment that groups its consumable is not in the Library (restore that equipment first).
- To delete deleted equipment permanently, use "Permanently delete deleted equipment…" under "Data usage and cleanup" on the "Settings" screen. Permanently deleted equipment cannot be restored. For details, see [the "Settings" screen](#settings).

## Consumable name and description

In the game, the Quartermaster in Devil's Crossing sells the equipment's consumable (the item that adds the illusion to your collection when used). "Consumable text…" changes its name and description. The details on the right show whether the default text is used or it has been changed.

- **"English (game in English)" and "Japanese (game in Japanese)" under "Consumable name"**: The consumable's name. Up to 64 characters.
- **"English (game in English)" and "Japanese (game in Japanese)" under "Description (text shown under the name)"**: The description shown under the name. Up to 400 characters.
- The text after "When blank:" under each field: the default text used when the field is blank. If a Japanese field is blank, the English field has text, and the equipment has no Japanese name, the English text is used.
- **"Clear all"**: Clears the four fields. Saving returns them to the default text.
- **"Save"**: Saves the text and closes the window. If the text has characters that cannot be used, an error is shown and the window stays open with your input.
- **"Cancel"**: Closes the window without saving.

- The characters = ^ { } \ and line breaks cannot be used. Use only ASCII letters, digits, and symbols in the English fields (the game's fonts in English have no other characters). If there are other characters, a warning appears after saving.
- The game wraps descriptions only at half-width spaces, so put a half-width space between phrases in Japanese descriptions, as the default text does (a long part without spaces is cut off before the end).
- The default descriptions are enclosed in double quotation marks (`"`), like the official descriptions.
- When you change the text, the equipment goes back to "MOD not built". The changed text appears in the game after you rebuild and install the MOD (how changed text looks in the game has not been confirmed).
- For equipment whose consumable is grouped into the consumable of another piece of equipment (the details on the right say so), "Consumable text…" cannot be clicked. Edit the text from the equipment that groups it.

## Remaking the appearance from text (experimental)

When "Appearance remake" is turned on under "Generation settings" → "Advanced settings" on the "Generate" screen, "Remake appearance from text…" appears in the details. It keeps the shape (pose) of the saved equipment's generation result, remakes only the colors, materials, and decorations from English text, and saves the result as new equipment. The original equipment does not change.

1. Select the equipment to start from and click "Remake appearance from text…".
2. The app switches to the "Generate" screen. Enter the text (in English) and click "Start generation".
3. Check the result and save it; it is added to the Library as new equipment.

- It requires the optional text components (TRELLIS-text-xlarge and CLIP) and acceptance of the CLIP notice on the "Generate" screen. If the components are missing, download them under "Setup" on the left.
- It cannot be used for equipment without a saved generation result (3D model). The text next to the button shows whether it can be used and what is missing.
- It has been tested with only 1 sample and 2 texts. The colors may not match the text, and small details of the shape may change.

## Notices at the top of the screen

- A notice that equipment made inside another app (such as Claude) is in a place this app cannot see (that app's virtualized folder): that equipment is not included in the MOD as is. Click **"Import…"**, then "Import" in the confirmation window, to import it into the Library with its internal IDs, in-game records, and name tags kept. The source files are left as they are, and equipment already in the Library is not touched. After importing, build and install the MOD.
- A notice that this app was started from inside another app (such as Claude): new equipment, MODs, and install records are saved in that app's virtualized folder and cannot be seen by the app when it is started normally. For everyday use, start the app from the desktop or File Explorer.

For other problems, see [Troubleshooting](#troubleshooting).

# The "MOD" screen {#mod}

Open this screen with "MOD" on the left (its page title is "Build and install the MOD"). It builds one MOD from all the equipment in the Library and installs it into Grim Dawn's mods folder. The screen has three sections: "1. Build the MOD", "2. Install to Grim Dawn", and "3. Sharing for multiplayer".

> "Build MOD" alone does not change the game. The MOD takes effect in the game once you place it in the mods folder with "Install to Grim Dawn…" and set `/basemods` in the Steam launch options. This app does not change the game's original files or the Steam launch options.

## Banners at the top

Depending on the situation, the following banners appear at the top of the screen.

- **"The Library has changed"**: Appears when equipment has been added, changed, or deleted in the Library since the MOD was last built, or when building the MOD failed for some equipment. Rebuild with "Build MOD" before installing.
- **"The install target (Grim Dawn folder) was not found"**: Appears when the install target folder cannot be found. The install status cannot be checked, and you cannot install or uninstall. Check "Grim Dawn install folder" on [the "Settings" screen](#settings) (or "MOD install target (for testing; normally blank)" if you use a test install target). If the folder is on an external drive, connect the drive and click "Refresh".
- **"Grim Dawn is running"**: Appears while the game is running. You cannot install or uninstall while it is running, so exit the game completely.
- **Game update banner**: Appears when a game update has changed the Quartermaster's stock (the official data that the MOD overwrites) since the MOD was built. As it is, the game's new stock would be overwritten with the old contents. The banner shows the changed tiers and what to do.
  - **"Rebuild the MOD"**: The same as "Build MOD". After rebuilding, install again with "Install to Grim Dawn…".
  - **"Install the latest build"**: Appears when the latest build matches the current game. The same as "Install to Grim Dawn…"; no rebuild is needed.
  - If the installed received MOD does not match the current game, no button appears. Ask the person who made the MOD to rebuild it with the current game and share it again, then receive and install it again.

## The "1. Build the MOD" section

- **"Build MOD"**: Builds the MOD (`database.arz`, `Text_XX.arc` for 13 languages, and `Items.arc`) from the equipment in the Library and saves it in the data folder. The mods folder is not changed. While the build runs, the progress and the current stage are shown. Development equipment (prototypes) is included only when "Also include development equipment (prototypes) in the MOD" is on in "Settings".
- **"Export to folder…"**: Saves the MOD files of the latest build, together with `README_GDSkin.txt`, which lists the file names and how to install them, to a folder you choose. Use it to install the MOD by hand. The app does not export into a folder that already has files with the same names, so choose an empty folder. You cannot export into the Grim Dawn folder (including its mods folder).
- **"Cancel"**: Stops a running MOD build or share file export. Installing, uninstalling, and removing a received MOD cannot be stopped partway.

Right below the heading, "Latest build:" is followed by the number of the last successful build, when it was built, the number of equipment items, and the number of warnings (if you have not built a MOD yet, "The MOD has not been built yet." is shown). Below that, the names and sizes of the MOD files and the database code (the code for `database.arz` alone; a rough guide to whether you can connect in multiplayer) are shown. The bold line at the bottom shows the result of operations such as building, installing, and uninstalling.

> Building the MOD uses Grim Dawn's game data (including the Ashes of Malmouth expansion) and the game's bundled `ArchiveTool.exe`. The MOD cannot be built if the Grim Dawn folder is not found.

## The "2. Install to Grim Dawn" section

- **"Install target"**: The Grim Dawn folder that the MOD is installed into (the MOD is placed in its mods folder). It is determined by "Grim Dawn install folder" in "Settings"; if that is blank, the folder is found automatically through Steam. If "MOD install target (for testing; normally blank)" is set in "Settings", that location and the "Test install target" mark are shown.
- **"Install status"**: Shows "Installed:" followed by the build number, the date and time of the install, and the number of equipment items of the MOD installed by this app. While a received MOD is installed, its file name, the date and time of the install, and the number of equipment items are shown; when nothing is installed, "Not installed by this app" is shown. If the latest build has not been installed yet, a line below asks you to install it (when the Library has changed or a game update banner asks you to rebuild, it asks you to rebuild with "Build MOD" before installing).
- **"Launch options"**: Shows whether `/basemods` is set in the Steam launch options. The app only reads the settings of the user currently logged in to Steam (or, if that cannot be determined, the user who logged in last) and never changes them. If you have just set the option while Steam is running, exit Steam and click "Refresh" to check it.
- **"How to set /basemods"**: Shows the steps for setting the launch option and how to use the MOD in the game.
- **"Game language"**: Shows the language that Grim Dawn is set to. For languages other than English and Japanese, it also says that whether the game reads the MOD's item names and descriptions (`Text_XX.arc`) has not been confirmed (with English and Japanese, the game was confirmed to read them).
- **"Game version"**: Shows the Steam build number of the game at the install target and compares the game with the one the installed MOD (or, if none, the latest build) was built with. The build number cannot be read for a game not installed through Steam or for a test install target. If Steam is waiting to update the game, that is shown as well. The result is one of the following.
  - The game is the same version as when the MOD was built.
  - The game was updated after the MOD was built, but the Quartermaster's stock that the MOD overwrites has not changed, so the MOD can be used as is.
  - The Quartermaster's stock has changed since the MOD was built. A MOD that matches the current game is needed.
  - There is no record of the version when the MOD was built, so the versions were not compared.
- **"Install to Grim Dawn…"**: Installs the latest build into the mods folder. The app first checks that the game is not running, checks for conflicts with other MODs and the files to back up, and shows the results in a confirmation window; clicking "Install" there installs the MOD. You cannot click it until you have built the MOD. For the steps, see [Installing a MOD and using it in the game](#mod-install).
- **"Uninstall…"**: Removes the files installed by this app and returns the mods folder to its state before the first install. You cannot click it if nothing was installed by this app. For details, see [Returning the game to its state before the MOD](#mod-restore).
- **"Refresh"**: Reads the install status, the mods folder status, and the other items again.
- **"Open mods folder"**: Opens the mods folder of the install target in File Explorer.

Under the buttons, a note appears when uninstalling would return to an old AI Skin MOD, or when nothing is installed by this app but the mods folder contains an AI Skin MOD with no record in this app (see [Returning the game to its state before the MOD](#mod-restore)).

> Only one `mods\database.arz` is read, so this MOD cannot be used together with other main-campaign MODs.

### The "mods folder status" table

The table lists, one per row, the 15 files directly in the mods folder that this app manages (`database.arz`, `Text_XX.arc` for 13 languages, and `Items.arc`) and any other `.arc` and `.arz` files directly in the folder.

- **"File"**: The file name.
- **"Owner"**: What the file belongs to (see the list below).
- **"Size"**: The file size in bytes.
- **"Description"**: Details of the classification (such as the number of the installed build, or examples of what another MOD's file contains).

"Owner" shows one of the following.

- **"None"**: The file does not exist.
- **"Installed by this app"**: The file was installed by this app, and its contents match the install record.
- **"Changed after this app installed it"**: The contents have changed since this app installed the file. Before installing or uninstalling, the app asks whether it may back up and replace the file.
- **"AI Skin MOD with no record"**: There is no record of the file in this app, but everything in it belongs to AI Skin (for example, a MOD placed by hand, or one whose install record was lost). It is backed up and then replaced when you install. If it was there before the first install, uninstalling returns to it.
- **"Another MOD or unknown file"**: A file of another MOD, or a file that cannot be read. Before installing, the app asks whether it may back up and replace the file (once it is replaced, that MOD no longer works correctly).
- **"Other file (loaded)"**: An `.arc` or `.arz` file directly in the mods folder that this app does not manage. This app does not touch it, but it is still loaded with `/basemods` (for example, files left over from another MOD).

The "Install backups" line below the table shows how many backups (copies of files from before changes) were made in `install\backups` in the data folder during installs, uninstalls, and similar operations, and their total size. The app never deletes backups automatically. To delete old backups, use "Delete old install backups…" under "Data usage and cleanup" on the "Settings" screen.

## The "3. Sharing for multiplayer" section

This section exports your MOD to one file (`.gdskinmod`) to give to others, and installs a MOD you received without rebuilding it. For how to use it, see [Sharing equipment and MODs](#share). Whether the appearance shows in multiplayer has not been confirmed in the game.

- **"Installed MOD"**: Shows whether the installed MOD is one you built or one you received. For a received MOD, its file name, the number of equipment items, its verification code, and the state it returns to when removed are shown.
- **"Verification code"**: The verification codes of the installed MOD and of the latest build. The code has 12 characters (for example `1A2B-3C4D-5E6F`) and is made from the names and SHA-256 hashes of all the MOD's files; if everyone's code is the same, everyone has the same MOD.
- **"Export the MOD to a share file…"**: Exports the latest build to a share file as is, without rebuilding.
- **"Import and install a share file…"**: Checks a share file you received, shows a confirmation window, and then installs it. The current MOD is replaced (it is backed up, so you can go back).
- **"Remove the received MOD and restore"**: Removes the received MOD and returns the mods folder to its state before the received MOD was installed. You can click it only while a received MOD is installed.
- **"Verification details (code for each file, comparison with the other person's code)"**: When expanded, it shows the verification codes of the installed MOD and the latest build and how they differ, a table of the code for each file ("File", "Installed MOD", "Latest build", and "Difference"), and the "Their code" field. Enter the verification code shown in the other person's app and click "Compare" to see whether it matches the installed MOD, the latest build, or a received MOD.

# Installing a MOD and using it in the game {#mod-install}

These are the steps from your saved equipment to using it in Grim Dawn. The fields and buttons of the screen are explained in [the "MOD" screen](#mod).

## Before you start

- Save at least one piece of equipment to the Library (see [the "Generate" screen](#generate)).
- You need Grim Dawn (Steam version) and the Ashes of Malmouth expansion (the MOD template uses Ashes of Malmouth data).
- Only one `mods\database.arz` is read, so this MOD cannot be used together with other main-campaign MODs.
- Before you install a MOD for the first time, it is recommended to copy your save folder as a whole to another location.
  - `Documents\My Games\Grim Dawn\save`
  - If you use Steam Cloud, also `Steam\userdata\<number>\219990\remote\save`

## Installation steps

1. Exit Grim Dawn completely (you cannot install while it is running).
2. Open "MOD" on the left and click "Build MOD" in the "1. Build the MOD" section. When it finishes, the bold line shows "MOD built" with the build number, the number of equipment items, and the number of warnings.
3. Click "Install to Grim Dawn…" in the "2. Install to Grim Dawn" section.
  - If the mods folder contains files of another MOD, or files changed since this app installed them, the "Another MOD's files are present" window appears first. Review the "Affected files"; if it is OK to replace them, select the agreement check box and click "Continue" (if there are both kinds, you are asked twice). Files of another MOD are backed up and then replaced, and uninstalling restores the original files. To keep using that MOD, click "Cancel".
4. In the "Install the MOD to Grim Dawn" window, check the contents and click "Install".
  - "Install target" and "Checks": Where the MOD is installed, and the results of the checks (that the game is not running, conflicts, and backups).
  - "Files to place", "Files to back up and replace", and "Files to delete (backed up)": The files that change in the mods folder. Files are backed up before being changed, and the placed files are checked against the records. If something fails partway, the changes are rolled back.
  - "Equipment changes": Compared with the MOD now in the mods folder, how many equipment items are kept, added, and removed. If some equipment will be removed, a list of it and a confirmation check box appear (you cannot click "Install" until you select it). The illusions of removed equipment can no longer be used in the game, including collected ones. To keep them, click "Cancel", add that equipment to the Library, and rebuild the MOD.
  - "Files that stay in the mods folder and are still loaded with /basemods (this app does not touch them)": Files of other MODs and similar files that remain in the mods folder after installing.
  - "Warnings": Notes such as `/basemods` missing from the Steam launch options, a game language other than English, or a game update.
5. In the "Installed" window, read "Using it in the game". "Install status" changes to "Installed".
6. Set `/basemods` in the Steam launch options. If the "Launch options" field shows that it is already set, you can skip this step. This app never changes the launch options.
  - In the Steam library, right-click Grim Dawn → "Properties"
  - Under "General" → "Launch Options", enter `/basemods` (if there are other options, add it at the end, separated by a space)
7. Start Grim Dawn from Steam. The MOD is loaded in the main campaign.

> If you set the launch option while Steam is running, the "Launch options" field in the app may not show it until you exit Steam. Exit Steam, then click "Refresh".

> If you try to close the app during an install or uninstall, a confirmation window appears. If you click "Wait and close", the app does not stop the operation partway; it waits for it to finish and then closes.

## Using the illusion in the game

1. Buy "Illusion: <name>" from the Quartermaster in Devil's Crossing (Ellis; Survivors reputation of Friendly or higher), close the shop, and right-click it in your inventory to add it to your collection (right-clicking an item in your inventory while the shop is open sells it back).
2. Equip something on the head, chest, shoulders, hands, legs, and feet. Illusions can only be applied to equipped slots.
3. At the Illusionist, apply your equipment's illusion to the head and invisible (transparent) illusions to the other slots.

> In a game set to Japanese, the consumable in step 1 has a Japanese name: the Japanese word for Illusion, followed by the equipment's Japanese name (or its English name if it has no Japanese name). This applies when "Use Japanese item names and descriptions for the game in Japanese" in "Settings" is on (it is on by default). The game set to Japanese was confirmed to read `mods\Text_JA.arc`.

> This MOD's items (the consumable sold by the Quartermaster, and the head and invisible-part illusions you choose at the Illusionist) have icons with "MOD" written on them (purple for the consumable, orange for the head, blue for the invisible parts; the medal's icon has not been checked in the game).

> You can change the name and description of the consumable sold by the Quartermaster with "Consumable text…" in the Library (the changed text appears in the game after you rebuild and reinstall the MOD; the game wraps descriptions only at half-width spaces, so put a half-width space between phrases in Japanese descriptions; how changed text looks in the game has not been confirmed).

> If you wear a medal, applying this MOD's invisible illusion to the medal as well should hide the badge on the chest (not confirmed in the game).

## When you add or change equipment

When the Library changes (for example, when you save, reconvert, rename, or delete equipment), the "The Library has changed" banner appears on the "MOD" screen. To bring the changes into the game, click "Build MOD" again and then "Install to Grim Dawn…". Clicking "Rebuild MOD" in the Library also switches to the "MOD" screen and starts building the MOD.

!> The MOD contains the equipment that was in the Library when it was built. Equipment you deleted from the Library disappears from the game when you install the rebuilt MOD, and its collected illusions can no longer be used.

## After a game update

The MOD contains a copy of the Quartermaster's stock (official data), so when a game update changes the stock, the MOD overwrites it with the old contents. When the game update banner appears on the "MOD" screen, rebuild with "Rebuild the MOD" on the banner, then install again with "Install to Grim Dawn…". If the latest build already matches the current game, installing it again with "Install the latest build" on the banner is enough. If you use a received MOD, ask the person who made it to rebuild it with the current game and share it again.

## Installing by hand

1. In the "1. Build the MOD" section, export the MOD files to an empty folder with "Export to folder…".
2. Put the exported `database.arz`, `Items.arc`, and `Text_XX.arc` for 13 languages (the names are listed in `README_GDSkin.txt`) into Grim Dawn's mods folder. If the mods folder has files with the same names, move them somewhere else before placing the new files.
3. Set `/basemods` in the Steam launch options.

> When you place the files by hand, existing files are not backed up and conflicts are not checked ("Install to Grim Dawn…" in the app does both automatically). No install record is kept either, so if the app has no record of an install, "Uninstall…" in the app cannot remove the MOD (see "If the app cannot remove the MOD" in [Returning the game to its state before the MOD](#mod-restore)).

## Using it in multiplayer

To show each other's appearance in multiplayer, everyone is expected to need the same MOD (the same `database.arz`); this has not been confirmed in the game. For how to make and install share files, see [Sharing equipment and MODs](#share).

# Returning the game to its state before the MOD {#mod-restore}

This app does not change Grim Dawn's original files; it only places the MOD files in the mods folder. It does not change the Steam launch options either. To return the game to its state before the MOD, use "Uninstall…" in the app to return the mods folder to its state before the first install, and, if needed, remove `/basemods` from the Steam launch options.

## Before you start

- Before you remove the MOD, it is recommended to copy your save folder (`Documents\My Games\Grim Dawn\save`; if you use Steam Cloud, also `Steam\userdata\<number>\219990\remote\save`) as a whole to another location.
- How illusions already in your in-game collection are shown after uninstalling has not been confirmed. What happens to this app's consumables and collected illusions when you start the game without `/basemods`, or load the character in the Crucible or a Custom Game, has not been tested either.

## Uninstall steps

1. Exit Grim Dawn completely (you cannot uninstall while it is running).
2. Open "MOD" on the left and check "Install status" in the "2. Install to Grim Dawn" section. If nothing was installed by this app, you cannot click "Uninstall…" (see "If the app cannot remove the MOD" below).
3. Click "Uninstall…".
  - If files installed by this app have been changed since, or files of another MOD have been placed at the locations being restored, the "Another MOD's files are present" window appears first. Review the "Affected files", select the agreement check box, and click "Continue"; those files are also backed up before being replaced.
4. In the "Uninstall the MOD" window, check the contents and click "Uninstall".
  - "Install target" and "Checks": Where the MOD is uninstalled from, and the result of the check that the game is not running.
  - "Files to remove (installed by this app)": Files that were not there before the first install, so they are deleted.
  - "Files to restore (from the backups)": Files that are returned to their contents from before the first install.
  - "Old AI Skin MOD that remains after uninstalling (from before the first install)": Appears only when an old AI Skin MOD was there before the first install (see "When an old AI Skin MOD remains" below).
5. The bold line in the "1. Build the MOD" section shows "Uninstalled. The mods folder is back to its state before the first install." You can check the current files in the "mods folder status" table.
6. If no other MOD uses `/basemods`, remove `/basemods` from the Steam launch options (in the Steam library, right-click Grim Dawn → "Properties" → "General" → "Launch Options").

> If uninstalling stops or fails, see [Troubleshooting](#troubleshooting). If writing fails, the mods folder is returned to its state before the operation.

## What uninstalling changes and what it leaves

- **Files in the mods folder**: The 15 files directly in the folder that this app manages (`database.arz`, `Items.arc`, and `Text_XX.arc` for 13 languages) are returned to their state before the first install. Files that were not there at that time are deleted, and files that were there (files of another MOD that you agreed to replace when installing, or an old AI Skin MOD) are restored from the copies backed up to `install\backups` in the data folder. Before anything is changed, the current files are also backed up to `install\backups`.
- **Not changed**: Other files and folders in the mods folder (files shown as "Other file (loaded)" and folders such as `mods\survivalmode`), the game's original files, and the Steam launch options are not changed.
- **The mods folder itself**: If the first install created the mods folder, the folder is also deleted when it becomes empty.
- **Library**: The "Installed" mark is removed from the equipment.
- **Kept in the data folder**: Your equipment, MOD builds, and install backups are kept. To use the MOD again, install it again with "Install to Grim Dawn…". Backups are not deleted automatically; to clean them up, use "Delete old install backups…" in "Data usage and cleanup" on [the "Settings" screen](#settings).

## When an old AI Skin MOD remains

Uninstalling returns to "the state before the first install". If the mods folder contained an AI Skin MOD with no record in this app (for example, a prototype) at that time, the folder returns to that MOD, and the game still loads it after uninstalling. This is shown under the buttons on the "MOD" screen and in the uninstall confirmation window. If you do not need it, exit the game, open the folder with "Open mods folder", and move those files somewhere else.

## Removing only a received MOD

To return to the state before a received MOD was installed (such as your own MOD), use "Remove the received MOD and restore" in the "3. Sharing for multiplayer" section (see [Sharing equipment and MODs](#share)). "Uninstall…" returns to the state before the first install even while a received MOD is installed.

## If the app cannot remove the MOD (no install record)

There is no install record if, for example, you deleted the data folder, installed with a data folder in another location (such as the virtualized folder used when the app is started from inside another app such as Claude), or placed files exported with "Export to folder…" by hand. In that case, "Uninstall…" cannot be clicked or stops with "No MOD has been installed by this app", and the files in the mods folder remain. Remove the MOD with these steps.

1. If you do not use other `/basemods` MODs, remove `/basemods` from the Steam launch options. This alone stops the game from loading the files directly in the mods folder.
2. To delete the files as well, exit Grim Dawn and check the "Owner" of each file in the "mods folder status" table on the "MOD" screen.
3. Delete only the `database.arz`, `Items.arc`, and `Text_XX.arc` files directly in the mods folder that are shown as "Installed by this app" or "AI Skin MOD with no record" (`Text_CS.arc` to `Text_ZH.arc` for 13 languages; MODs made with earlier versions have only `Text_EN.arc` and possibly `Text_JA.arc`).

!> Do not delete files shown as "Another MOD or unknown file". Do not delete folders such as `mods\survivalmode` (the Crucible DLC) either.

## Before you delete the data folder

!> Always run "Uninstall…" before you delete the data folder (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`). If installing replaced files of another MOD, the original files exist only in `install\backups` in the data folder, and deleting the data folder makes it impossible to restore them.

To remove the app itself, see [Uninstalling the app](#uninstall).

# Sharing equipment and MODs {#share}

There are two kinds of files that you can exchange with other people.

- **Equipment package (`.gdskin`)**: The selected equipment in one file. The recipient imports it into their Library and can build a MOD together with their own equipment. Use "Export…" and "Import file…" on the "Library" screen.
- **MOD share file (`.gdskinmod`)**: A built MOD put into one file as is, without rebuilding. It is for everyone using the same MOD in multiplayer; the recipient installs it byte for byte. Use "3. Sharing for multiplayer" on the "MOD" screen (for its fields, see [the "MOD" screen](#mod)).

!> How it works in multiplayer has not been tested. It has not been confirmed whether you see each other's appearance when everyone has the same MOD, or whether you can connect when the MODs differ.

!> Do not use files received from people you do not know. The hash (SHA-256) check during import only tells you whether the file was corrupted or partly modified along the way. It cannot detect a file whose contents and records were remade together, such as changes made by the creator (a hash is not a signature).

## Exporting equipment (.gdskin)

1. On the "Library" screen, select the equipment to export. Hold down Ctrl or Shift while clicking to select several pieces of equipment.
2. Click "Export…".
3. In the confirmation window ("Export equipment"), check the equipment to export and what the file contains.
4. To also give the input images and prompts, check "Also include the input images and prompts (the images and text used for generation; the recipient can use them to regenerate. Not included by default)". This check box appears only when the selected equipment has input images or a prompt.
5. Click "Choose where to save…" and choose the location and file name.
6. The "Exported" window shows the file location, size, and package ID. Give the file to the other person and ask them to import it with "Library" → "Import file…".

- What the file contains: the equipment records (internal ID, name, in-game record paths, tags, creation time), the male and female `.msh` and `.tex`, the check images and 3D previews (male and female), the version of the tool that made them, and hashes (SHA-256) of all files.
- Not included: MOD status, error history, your PC's folder names, original file names, and the generated GLB. The input images and prompts are included only when you check the box.
- If you include the input images and prompts, the recipient can use them to regenerate. Be careful who you give the file to.
- Equipment whose conversion has not finished cannot be exported.
- When you export equipment whose appearance was remade, the internal ID of the original equipment and the SHA-256 of the original 3D model are included in the record.

## Importing equipment (.gdskin)

1. On the "Library" screen, click "Import file…" and select the `.gdskin` you received.
2. The app checks the file (hash verification). If there is a problem, nothing is imported and the reason is shown.
3. In the confirmation window ("Import equipment"), check the contents.
  - "File information": the file name, creation time, package ID, version of the tool that made it, whether input images and prompts are included, and the extracted size. The package ID is calculated from the contents; files with the same contents have the same package ID.
  - "New equipment to import": equipment that is not in your Library.
  - "Equipment with the same internal ID but different contents": equipment whose internal ID is already in your Library but whose name, consumable name and description, 3D model and textures, creation time, consumable grouping, or record paths differ. The differences and the name in your current Library are shown.
  - "Equipment skipped because it is already present": the same equipment is already in your Library, so it is not imported.
  - "Equipment not imported": development equipment (prototypes) is not imported.
4. To replace all the equipment that has different contents with the version in this file, check the replace check box (it appears only when there is equipment with different contents, and it is unchecked by default). If you leave it unchecked, your current equipment is kept.
5. Click "Import".
6. After importing, build and install the MOD on the "MOD" screen.

- The internal IDs, in-game record paths, tags, and creation times are kept as in the file.
- Replaced equipment is moved to `library\trash` in the data folder. It appears in the "Deleted equipment…" list, but it cannot be restored while equipment with the same internal ID is in the Library.
- If the 3D model and textures of the replaced equipment are the same, its local-only generation result, prompt, and input images are carried over. If they differ, they cannot be carried over, and the confirmation window says so (without the generation result, "Reconvert" is no longer possible either).
- Newly imported equipment has no generation result (GLB), so "Reconvert" and "Remake appearance from text…" cannot be used for it.
- If all the equipment in the file is already in your Library with the same contents, "Nothing to import" appears.
- Nothing is imported in the following cases: the file is corrupted or is not a GD Skin Studio share file; the contents do not match the records (hashes) because the file was corrupted or modified along the way; it contains unsafe contents (dangerous file names, encryption, or extreme compression); it is too large; it was made with a newer version of GD Skin Studio (update the app, then import it); or it contains equipment whose record has a problem or whose record paths or tags overlap with other equipment in your Library ("This file cannot be imported").
- If you select a MOD share file (`.gdskinmod`), the app tells you to use "Import and install a share file…" on the "MOD" screen.

## Exporting a MOD to a share file (.gdskinmod)

1. On the "MOD" screen, click "Build MOD" to build the MOD (to use it in your own game too, install it with "Install to Grim Dawn…").
2. Click "Export the MOD to a share file…" in "3. Sharing for multiplayer".
3. If the Library has changed since the MOD was last built, a confirmation window appears. "Export anyway" exports the latest build from before the changes. To include the changes, click "Cancel", rebuild the MOD, and then export it.
4. Choose where to save (the default file name is `GDSkin_<build ID>.gdskinmod`).
5. The "Exported the share file" window shows the file location, the size and verification code, the database code, and "What the other person needs to do".

- The files of the latest successful build (`database.arz`, `Items.arc`, and `Text_XX.arc` for each language) are put into the file as they are, without rebuilding.
- The share file is a zip file. It also contains a `README.txt` (in Japanese and English) with the verification code and how to install the MOD without the app.
- What the other person needs to do: match your Grim Dawn expansions and version, and the Steam launch option `/basemods`; install this file with "MOD" → "Import and install a share file…" in GD Skin Studio; and check that the verification code after installing is the same as yours.
- To cancel while the file is being exported, click "Cancel" in "1. Build the MOD".

## Installing a received MOD (.gdskinmod)

1. Match your Grim Dawn expansions and version, and the Steam launch option `/basemods`, with the person who made the MOD. Exit Grim Dawn.
2. On the "MOD" screen, click "Import and install a share file…" and select the `.gdskinmod` you received.
3. The app checks the file. It is extracted into the data folder; the mods folder is not changed at this point. A MOD that contains things GD Skin Studio never creates (such as changes to official data) is not installed, and "This MOD cannot be installed" appears.
4. In the confirmation window ("Install a received MOD"), check the contents.
  - "Verification code (whole MOD)": check that it matches the code shown in the other person's app.
  - "Comparison with the installed MOD and your latest build": shows whether it is the same, differs only in appearance and names, or has a different database.
  - "Points to check": items whose contents differ from the official data or from the values GD Skin Studio creates (caused by a different game version or by changes made by the creator). They do not stop the installation, so check them before you decide.
  - "Equipment in this MOD": the equipment in the MOD.
  - "Equipment now in the game but not in this MOD": equipment that can no longer be used in the game after installing. When there is none, "All equipment of the current MOD is also in this MOD" is shown.
  - "Game setup": warns when the expansions or the size of the game database differ from theirs, or when Grim Dawn is not found on this PC.
  - "Install target", "Files to place", "Files to back up and replace", and so on: the same checks as in a normal installation.
  - "File information": the share file's name, creation time, and package ID, their build, the files it contains and their sizes, the database code, and the code for each file.
5. Check the confirmation box (such as "I understand that the current MOD will be replaced by the other person's MOD") and click "Install". You cannot click it until you check the box.
6. The "Installed the received MOD" window shows the verification code. If it is the same as the other person's code, you have the same MOD.

- The received MOD replaces the MOD currently in the mods folder. Only one `database.arz` in the mods folder is read, so your equipment cannot be used in the game unless it is in this MOD. To use both, exchange equipment packages and build a MOD with the same set of equipment.
- The current files are backed up, so you can go back with "Remove the received MOD and restore".
- The equipment in a received MOD is not added to your Library. While a received MOD is installed, none of the equipment in your Library shows "Installed".
- If the file is corrupted or modified, is of a different type (an equipment package), or was made with a newer version of GD Skin Studio, "Cannot read the share file" appears and nothing is installed.
- Other MODs' files and a running game are handled the same as in a normal installation (see [Installing a MOD and using it in the game](#mod-install)).

## Removing a received MOD and restoring

While a received MOD is installed, "Remove the received MOD and restore" on the "MOD" screen returns the mods folder to its state before the received MOD was installed.

1. Exit Grim Dawn.
2. Click "Remove the received MOD and restore".
3. In the confirmation window ("Remove the received MOD"), check "Restore to" and the equipment that comes back to or disappears from the game, then click "Restore".

- "Restore to" is the state before the received MOD was installed (your MOD, a MOD received earlier, or the state before this app's MOD was installed). The files backed up at the time of that installation are restored.
- "Uninstall…" returns to the state before the first install. For the difference, see [Returning the game to its state before the MOD](#mod-restore).
- The received MOD remains in the data folder, so you can install it again by importing the share file again.

## Checking with the verification code

The verification code is a 12-character code (for example `1A2B-3C4D-5E6F`) made from the names and SHA-256 hashes of all the MOD's files (`database.arz`, `Items.arc` with the appearance, and `Text_XX.arc` with the names). If everyone's code is the same, everyone has the same MOD.

- The "Verification code" field on the "MOD" screen shows the codes of the installed MOD and the latest build. The code also appears in the window shown after exporting a share file, in the confirmation and completion windows for installing a received MOD, and in the `README.txt` inside the share file.
- The database code is made from `database.arz` alone. As a rough guide to whether you can connect, it is shown in the latest build field and in the confirmation windows. If a MOD is rebuilt with only the names or appearance changed, the verification code changes but the database code stays the same.
- The verification code only tells you whether the files are the same. Whether you can connect in multiplayer and whether the appearance is shown have not been tested.

### Verification details

"Verification details (code for each file, comparison with the other person's code)" is a collapsed section inside "3. Sharing for multiplayer" on the "MOD" screen. Use it to find which files differ when the verification codes do not match.

- It shows the verification codes of the installed MOD and the latest build, and whether the two are the same, differ only in appearance and names, or have different databases.
- The table of codes for each file: it has the "File", "Installed MOD", "Latest build", and "Difference" columns. "Difference" shows "Different", "Installed only", or "Latest build only". The code of `database.arz` is the same as the database code.
- **"Their code"**: Enter the verification code shown in the other person's app. The - separators and spaces are optional, and upper and lower case are treated the same.
- **"Compare"**: Compares the code you entered with the verification codes and database codes of the installed MOD, the latest build, and received MODs, and shows the result.
  - When it matches the installed MOD ("Match: it is the same as the installed MOD."): you have the same MOD installed as the other person.
  - When it matches the latest build or a received MOD: it is not installed yet. Install the latest build with "Install to Grim Dawn…", or install a received MOD by importing its share file again.
  - When only the database code matches: you should be able to connect, but check with the verification code whether the appearance and names are also the same.
  - When nothing matches: your MOD differs from theirs. Receive their share file and install it, or install the same MOD, then check again.
  - When the code has the wrong format, the app asks you to enter 12 alphanumeric characters (0-9, A-F).

If `database.arz` is the same and only the appearance (`Items.arc`) and names (`Text_XX.arc`) differ, you are expected to be able to connect, but each of you sees different appearances or names for the equipment (not confirmed in the game).

## Notes on multiplayer

- How it works in multiplayer has not been tested. To show each other's appearance, everyone is expected to need the same MOD (the same `database.arz`). MODs with different sets of equipment may prevent joining or show mismatched appearances.
- Match your Grim Dawn expansions and version with the other players. The install confirmation window for a share file tells you when the expansions or the size of the game database differ.
- You can also exchange equipment packages so that everyone builds a MOD with the same set of equipment. In that case, also match the game version, the expansions, and the GD Skin Studio version, and after building, check that everyone's verification code is the same (whether MODs built on different PCs come out the same has not been confirmed).
- If Grim Dawn is updated while you use a received MOD and the Quartermaster's stock changes, a warning appears on the "MOD" screen. Ask the person who made the MOD to rebuild it with the current game and share it again, then receive and install it again. If you replace it with your own MOD, your verification code will no longer match theirs.
- Before you install a MOD for the first time and before you remove a MOD, it is recommended to back up your saves (see [Installing a MOD and using it in the game](#mod-install)).

# The "Log" screen {#log}

Shows a record of operations and processing in the app. Use it to find out what happened when something does not work.

The text and images you enter and your user folder name are not written to the log. Your user folder is replaced with `%USERPROFILE%`, and in the records of generation, the text you entered and the file names and paths of images and GLB files are replaced with `<input>`.

## Filter and buttons

- **"Show only warnings and errors"**: When this is on, the "Activity" tab shows only warning (`WARN`) and error (`ERROR`) lines.
- **"Refresh"**: Reloads the contents of the "Backend log file" tab. It is available only while the app is connected to the backend.
- **"Open log folder"**: Opens the folder that contains the log files in File Explorer. The location of the folder is shown to the right of the button.

## The "Activity" tab

Lists the records since the app started, oldest first (new lines are added at the bottom). Each line shows the time, the level, the source, and the message.

- Level: `INFO` (information), `WARN` (warning), or `ERROR` (error). `WARN` and `ERROR` are shown in a different color.
- Source: `[app]` is a record from the app's screens, and `[backend]` is a progress message or warning from the backend (the process that runs generation, conversion, and MOD builds).
- When an error window is shown, its contents are also recorded as an `ERROR` line.
- The list keeps up to the 3,000 newest lines. The list starts empty each time you start the app (earlier records remain in the log files).

## The "Backend log file" tab

Shows up to the last 400 lines from the two newest backend log files (`backend-<date>.log`). It is reloaded when you open the "Log" screen and when you click "Refresh".

- **"Copy"**: Copies the text shown to the clipboard.

## Log files

The logs are saved in the `logs` folder of the data folder (by default `%LOCALAPPDATA%\GrimDawnSkinMOD\logs`).

- `app-<date>.log`: Records from the app's screens (the `[app]` lines in "Activity").
- `backend-<date>.log`: Records from the backend. Each line is one entry with the date and time, the level, and the message (for an error, the error code is also written).
- `jobs` folder: Detailed output of each operation, such as a generation, a conversion, or a MOD build (one file per operation).
- Log files older than 14 days are deleted automatically when the backend starts.
- If the backend stops after it has started, the end of the backend's output at that time is kept in `app-<date>.log` and in an `ERROR` line in "Activity".
- If the app cannot write to its log file (for example, because the file is read-only), a warning is shown in "Activity", and while the file cannot be written, records are kept only in the list on screen.

> When you report a problem, check that the logs do not contain your folder names or similar information before attaching them.

## Checking an error

1. Read the message and the suggested fix in the error window. Expand "Details (technical information)" to see technical information and the error code.
2. Click "Open log folder" in the error window to open the log folder.
3. On the "Log" screen, turn on "Show only warnings and errors" to check only the warning and error lines.
4. On the "Backend log file" tab, check the lines with the same time and the lines before and after them.

For common problems and how to solve them, see [Troubleshooting](#troubleshooting).

# The "Settings" screen {#settings}

This screen holds the settings for the Grim Dawn location, 3D generation, data handling, the display language, and more. The left side of the screen has the settings fields, data usage and cleanup, and About; the right side has the items installed by the setup and their licenses, TRELLIS runtime management, and the environment check.

After you change fields or check boxes on the left side, click "Save settings".

> The generation settings are in "Generation settings" on the "Generate" screen. When you start a generation, the values at that time are saved as the defaults for next time (see [The "Generate" screen](#generate)).

## Grim Dawn

- **"Grim Dawn install folder"**: The folder where Grim Dawn (Steam version) is installed. If left blank, it is found automatically through Steam. Game data is only read, never changed. If the folder you specify has no `database\database.arz`, Grim Dawn is treated as not found. You can choose the folder with "Browse…".
- **"Detect"**: Searches the Steam libraries for Grim Dawn and shows the locations found below the field. When Grim Dawn is found, the field is cleared (if you save with the field blank, automatic detection is used). If it is not found, specify the folder.
- **"MOD install target (for testing; normally blank)"**: If left blank, the MOD is installed into the `mods` folder of the install folder above. Specify this only to use another folder as a stand-in for the Grim Dawn install folder when testing (the folder needs `database\database.arz` and `ArchiveTool.exe`; their contents can be empty). When the required files are present, "MOD install target" in the environment check shows it as a test install target. You can choose the folder with "Browse…".

## 3D generation

- **"TRELLIS runtime"**: The folder of the TRELLIS runtime used for 3D generation. If left blank, the app looks for `trellis-runtime` in the data folder (on a PC where that location cannot be used, for example because your user folder name contains full-width characters, it also looks in `%SystemDrive%\GDSkinStudio\trellis-runtime`). When the setup downloads the runtime, this field is set to match the destination (it stays blank when the runtime is downloaded to `trellis-runtime` in the data folder). To use an existing TRELLIS runtime, specify a folder that has `venv\Scripts\python.exe` inside it. You can choose the folder with "Browse…". Download and check the runtime and models under "Setup" on the left (see [The "Setup" screen](#setup)).
- **"Reference meshes (required for conversion)"**: Shows whether the game's official body meshes (6 files) used for conversion are ready. If they are corrupted, extract them again with "Prepare from the game". If they cannot be opened, close or pause antivirus, backup, or sync software, or check your permission to read them, then try again ("Prepare from the game" does not fix this).
- **"Prepare from the game"**: Opens Grim Dawn's `Creatures.arc` and `Items.arc` read-only, extracts the 6 files used for conversion, and saves them in the data folder (the game files are not changed or copied). Click "Start" in the confirmation window to begin; the progress is shown. If you changed "Grim Dawn install folder" and have not saved it yet, the settings are saved first (the button in the confirmation window is "Save and start").
- **"Cancel"**: Stops preparing the reference meshes.
- **"CLIP notice (generation from text)"**: Shows whether you have accepted the notice in the model card of CLIP (OpenAI), which is used for generation from text, and when. Generation from images is available without accepting it.
- **"Read the notice…"**: If you have not accepted the notice yet, it is shown in the display language. Check "I have read the above and agree" and click "Agree" to record your acceptance. If you have already accepted it, the app tells you when you did. The acceptance is recorded in the TRELLIS runtime folder, so it cannot be recorded if the TRELLIS runtime is not found.

## Data and display

- **"Also save the input image when saving equipment (used for regeneration)"**: This is on by default. It sets the initial state of "Save the input image with the equipment (used for regeneration)" on the "Generate" screen. The saved input image is used by "Regenerate" in the Library.
- **"Keep intermediate generation files (drafts) after saving"**: This is off by default. When it is off, the draft is deleted when you save it as equipment. When it is on, the draft is kept after saving (kept drafts can be deleted in "Data usage and cleanup").
- **"Use Japanese item names and descriptions for the game in Japanese (puts the Japanese names into Text_JA.arc; when off, the English names are used)"**: This is on by default. The setting is used when the MOD is built, so after changing it, build the MOD again and install it for the change to take effect.
- **"Also include development equipment (prototypes) in the MOD"**: This is off by default. It also puts prototype equipment made during development (development equipment) into the MOD. Equipment made in the app is included in the MOD regardless of this setting. A MOD built with development equipment cannot be exported to a share file.
- **"Data folder"**: Where equipment, MOD builds, install records, and logs are stored. It is kept when the app is updated. Its location cannot be changed on this screen (see [Where the app keeps its data](#files)).
- **"Open"**: Opens the data folder in File Explorer.
- **"Display language"**: Choose "日本語" (Japanese) or "English" (the default is Japanese). Click "Save settings" and restart the app for the change to take effect (the backend restarts with the app).
  - With English, all text the app shows is in English, including messages from the backend, "Details (technical information)" in error windows, the environment check, the CLIP notice, and newly written log lines.
  - Log lines written before the switch and text you typed (such as names) stay as they are. Error messages from Windows in "Details (technical information)" of error windows are in the display language of Windows.
  - The English wording follows the game's official English terms but has not been reviewed by native English speakers.
- **"Backend Python (automatic if blank)"**: The location of the `python.exe` that runs the backend. If left blank, it is found automatically (the distributed app uses `python\python.exe` in the extracted folder). If there is no `python.exe` at the location you specify, the automatically found Python is used and a warning is written to the log. You can choose `python.exe` with "Browse…". Changes take effect after the app is restarted (while the app is not connected to the backend, they also take effect when you click "Restart backend" in the banner at the top of the screen).

## Saving the settings

- **"Save settings"**: Saves the changes to the fields, check boxes, and display language on the left side, and shows the result to the right of the button. Saving also runs the environment check again.
- For the folder fields ("Grim Dawn install folder", "MOD install target (for testing; normally blank)", and "TRELLIS runtime"), specify an absolute path, such as `C:\...` (relative paths cannot be used).
- While the app is not connected to the backend, only the display language and the Python location are saved (other settings are not saved).
- The display language and the Python location are saved in `ui_settings.json`, and the other settings in `backend_settings.json` (both in the `app` folder of the data folder). If a message says that they could not be saved, check the error, that the file is not read-only, and that there is free disk space, then click "Save settings" again.

> "Prepare from the game" and the buttons in "Data usage and cleanup" and "TRELLIS runtime management" act right away when you click them (you do not need to click "Save settings").

## Data usage and cleanup

Click the heading to open it. When you open it for the first time, the app checks and shows the total size of the data folder and the free space on the drive, the size of each item, and the number of items cleanup can delete. The TRELLIS runtime in the data folder is not counted (it is handled in "TRELLIS runtime management").

The items are "Equipment (MOD assets, previews)", "Saved input images", "Deleted equipment (trash)", "MOD builds", "Install records and backups", "Generation drafts", "Received MODs and temporary share files", "Reference meshes", "Logs", and "Other (caches, settings, etc.)". The cleanup buttons act only when you click them, and nothing is deleted automatically.

- **"Check usage again"**: Checks the sizes and numbers again.
- **"Delete old builds…"**: Deletes MOD builds. The installed build, the build of the state before the current install (the state that "Remove the received MOD and restore" returns to), and the latest successful build are kept. Deleted builds can no longer be used for installing, exporting to a folder, or sharing.
- **"Delete old install backups…"**: Deletes files that were backed up before installing, uninstalling, and similar operations. The state before the first install (which uninstalling returns to), the state before the current install (which "Remove the received MOD and restore" returns to), and the newest 5 are kept. You can no longer go back to the states saved in the deleted backups.
- **"Permanently delete deleted equipment…"**: Permanently deletes equipment that you deleted in the Library. It can no longer be restored from "Deleted equipment…" in the Library.
- **"Delete saved input images…"**: Deletes the input images saved with equipment. The MOD and conversion results do not change. Images used in the image fields of the "Generate" screen are kept. To regenerate equipment whose input image was deleted, select the image again. You also can no longer include input images in equipment share files.
- **"Delete failed, interrupted or saved drafts…"**: Deletes failed or interrupted drafts and drafts kept after they were saved as equipment. The draft shown on the "Generate" screen, finished drafts that have not been saved yet, and drafts being processed are not deleted.

### How to clean up

1. Click a cleanup button. A confirmation window lists what will be deleted and what will be kept (with the reasons). If there is nothing to delete, the app tells you so.
2. Check the list, select "I understand that deleted items cannot be restored", and click "Delete".
3. The number of deleted items and the space freed are shown.

!> Deleted items cannot be restored.

> While a MOD build, an installation, or an equipment change is running, builds, backups, deleted equipment, and input images cannot be cleaned up. Try again after it finishes.

## About

Shows the app name and version and the copyright notice. This app is provided under the MIT License. Third-party software that is bundled or downloaded is covered by its own license, not MIT.

- **"License (MIT)"**: Shows the full text of this app's license (`LICENSE.txt`).
- **"Third-party licenses"**: Shows the third-party copyright notices and licenses.

## Setup (installed items and licenses)

At the top, the destination of the TRELLIS runtime and whether the setup is completed (and when) are shown. Below that, each item that the setup downloads is listed with its state ("Installed", "Not installed", "Partial", or "Different version") and its license name. Some items are marked "Includes GPL", "Includes files under a non-commercial license", or "Has a notice (acceptance required)".

- **"Full license"**: Shows the license name and source of the item and the full license text. Click "Close" to close it.
- **"Open setup"**: Opens the "Setup" screen.
- **"Redo setup"**: Redoes the setup from the first step. Installed items are verified and reused, and only missing items are downloaded. You can also add the text → 3D items here.

> In items marked "Includes files under a non-commercial license", the parts under the non-commercial license (part of the kaolin package) may be used only non-commercially (for research or evaluation purposes). Use them only within the terms of that license.

> While the setup is downloading, redoing the setup and re-verifying, reinstalling, or removing the TRELLIS runtime are not available.

## TRELLIS runtime management (re-verify, reinstall, remove)

Click the heading to open it. It shows whether the destination of the TRELLIS runtime in use was created by the app's setup. Only destinations created by the app's setup can be managed here (for an existing TRELLIS runtime and similar cases, the reason is shown and nothing is changed). The target is the saved destination (if you changed the "TRELLIS runtime" field and have not saved it yet, the confirmation window also says so). Every operation runs only when you click it, and while it runs, the progress and "Cancel" are shown.

### Re-verify / reinstall

Use these when generation stops with "Could not load the models" or a failed self-test. For both, the files that are downloaded again come from their sources over the internet.

- **"Re-verify models…"**: Re-reads every file of the models and program sources, verifies them with SHA-256, and downloads again from their sources only the files that do not match. Files that match are not rewritten. This takes a few minutes. Click "Re-verify" in the confirmation window to start.
- **"Reinstall Python and packages…"**: Downloads Python, PyTorch, and the packages again from their sources, replaces them, and finally runs a self-test (up to about 4.2 GB is downloaded). The confirmation window shows the download size and the free space needed temporarily on the destination drive. Click "Reinstall" to start. The replacement happens only after the download and extraction finish, so the current environment stays as it is even if the connection drops. This takes a while.

> If there is no record of acceptance of the licenses and notices, "Reinstall Python and packages…" stops and asks for acceptance, and "Re-verify models…" does not download again the files that do not match. Accept them on the "Setup" screen, then download again.

### Remove

- **"Remove the text → 3D models…"**: Removes TRELLIS-text-xlarge and CLIP, which are used for text → 3D. Generation from images keeps working. To use text → 3D again, select the text → 3D items in Setup and download them again. This is available only when the text → 3D items are installed.
- **"Remove the whole TRELLIS runtime…"**: Removes the whole TRELLIS runtime that the app downloaded (Python, PyTorch, packages, models, etc.). After removal, 3D generation is unavailable until you download it again in Setup. When everything has been removed and the "TRELLIS runtime" field was set to this destination, the field is reset to blank (automatic).

The confirmation window shows what will be removed (with sizes and numbers of files), what will be kept (files and folders not created by the app), and the space that will be freed. Check "I confirm that these may be removed" and click "Remove" to start. Both removals also remove the record of acceptance of the CLIP notice (you accept it again when you download again). Only what the app downloaded is removed; other files you placed there are kept. The Library, built MODs, and settings are not removed.

> If a message says that some files or folders could not be removed, another app may have them open. Close that app and try removing them again.

> While 3D generation or conversion is running or queued, or while a background removal check is running, re-verifying, reinstalling, and removing are not available.

## Environment check

Shows the results of checking whether everything needed to use the app is in place. The date and time of the check are shown at the top. Each item shows a check mark (no problem), a warning mark (there is a caution), or an × mark (missing or unusable), together with what was found.

- **"Check again"**: Checks again. Click it after setting the Steam launch option, after exiting Grim Dawn, and so on.

### Items shown

- **"OS"**: Checks that the OS is 64-bit Windows.
- **"GPU", "GPU driver", "GPU generation (compute capability)", and "GPU memory"**: The NVIDIA GPU used for 3D generation. Driver R570 or later, compute capability 6.0 or higher, and 12 GB or more of GPU memory are required (less than 16 GB of GPU memory or compute capability below 7.0 is shown as a caution).
- **"Visual C++ Redistributable"**: Required for 3D generation. If it is reported as old or missing, install the latest version from `https://aka.ms/vc14/vc_redist.x64.exe` (administrator rights are required). GLB conversion and MOD builds are not affected.
- **"Conversion and MOD build components"**: The components bundled with the app that are used for conversion and MOD builds.
- **"TRELLIS runtime", "TRELLIS source", "Generation script", "Model (image → 3D)", "DINOv2 (image → 3D)", and "Background removal model"**: What is used for 3D generation from images. Without the background removal model, only images with a transparent background can be used.
- **"Model (text → 3D)", "CLIP (text → 3D)", and "Acceptance of the CLIP notice"**: What is used for 3D generation from text (optional). Generation from images is available without them.
- **"Grim Dawn", "ArchiveTool.exe", and "TextureCompiler.exe"**: The game's install folder and the game's bundled tools. If a tool is missing, you can restore it in the Steam library: Grim Dawn's Properties → "Installed Files" → "Verify integrity of game files".
- **"Expansion Ashes of Malmouth"**: Required to build the MOD.
- **"Reference meshes"**: Required for conversion (prepare them with "Prepare from the game").
- **"Temporary working folder"**: The working folder used for the check with the game's bundled ArchiveTool when the MOD is built. If no usable folder is found, the check with ArchiveTool is skipped (the check with the app's own reader is still done).
- **"Data and temporary folder paths"**: Checks that the paths of the data folder, the temporary folder, and the TRELLIS runtime are not too long and do not contain characters that can cause problems (such as characters that the game's bundled tools cannot handle).
- **"MOD install target"**: The Grim Dawn folder into which the MOD is installed.
- **"Launch option /basemods"**: Checks whether `/basemods` is set in the Steam launch options. It only reads the settings of the user currently logged in to Steam (if that cannot be determined, the user who logged in last). Right after you set the option while Steam is running, exit Steam and then click "Check again" (see [Installing a MOD and using it in the game](#mod-install)).
- **"Game running"**: Checks whether Grim Dawn is running. The MOD cannot be installed or uninstalled while the game is running.
- **"Data folder" and "Data folder (how the app was started)"**: Checks that there is only one data folder. If the app was started from inside another app (such as Claude), it tells you that new files are saved in a virtualized folder and cannot be seen by the app when it is started normally. Equipment in another app's virtualized folder can be imported with "Import…" in the Library.
- **"First-run setup"**: Shows whether the first-run setup is completed.
- **"Free space"**: The free space on the drive of the data folder (less than 5 GB is shown as a caution).

# Where the app keeps its data {#files}

The app keeps the app itself (the folder where you extracted the ZIP) and your data (the data folder) in separate places. The data folder is kept when the app is updated.

## Locations

- **Extracted folder** (for example `C:\Tools\GDSkinStudio`): the app itself (`GDSkinStudio.exe`, the bundled Python, the English `README.en.txt` and Japanese `README.txt` documents, etc.). Replace it as a whole when updating.
- **Data folder** (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`): stores your equipment, MOD builds, install records and backups, settings, logs, and more.
- **TRELLIS runtime and models** (by default `trellis-runtime` in the data folder): what the setup downloaded (about 13 GB, or about 19 GB with the text → 3D items). If the runtime cannot be placed inside the data folder, for example because your user folder name contains full-width characters, the default is `%SystemDrive%\GDSkinStudio\trellis-runtime` (`C:\GDSkinStudio\trellis-runtime` on most PCs). You can also choose another location during the setup.
- **Grim Dawn's mods folder** (`mods` in the Grim Dawn install folder): the installed MOD (`database.arz`, `Items.arc`, and `Text_XX.arc` for 13 languages). Uninstalling returns it to its state before the first install.

The current data folder is shown in "Data folder" under "Data and display" on the "Settings" screen. Click "Open" to open it in File Explorer. The location of the TRELLIS runtime is shown after "Destination:" under "Setup (installed items and licenses)" on the "Settings" screen. "Open log folder" on the "Log" screen opens the log folder.

## What is in the data folder

- `library\`: the equipment in the Library. Each item has the meshes and textures that go into the MOD, previews, conversion reports, and saved input images.
  - `library\trash\`: equipment deleted from the Library. It is moved here instead of being deleted permanently, so you can restore it with "Deleted equipment…" in the Library.
- `work\`: generation drafts (generation and conversion results before they are saved as equipment). A draft is deleted when you save it as equipment, discard it, or replace it with the result of a new generation (when "Keep intermediate generation files (drafts) after saving" in Settings is on, it is kept after saving). Failed and interrupted drafts are not deleted automatically (you can delete them in "Data usage and cleanup").
- `builds\`: MOD builds (one for each time you build the MOD).
- `install\`: install records and backups of files from before installing (`install\backups`, including the original files of other MODs that were replaced).
- `refs\`: reference meshes (6 body mesh files extracted from your Grim Dawn).
- `cache\`: copies of files read from the game (templates) that are used to build the MOD. If they are deleted, they are created again when needed.
- `shared\`: received MODs (the contents of `.gdskinmod` share files read with "Import and install a share file…").
- `logs\`: logs (`app-<date>.log`, `backend-<date>.log`, and detailed logs for each task in `jobs\`). Logs older than 14 days are deleted automatically at startup. The contents of the images and text you enter and your user folder name are not written to the logs.
- `app\`: settings (screen settings such as the display language, the Python location, and the window size, and backend settings such as the Grim Dawn location and the generation settings) and setup records (acceptance, completion, etc.).
- `webview\`: data of the 3D preview (WebView2), such as caches.
- `tmp\`: temporary work for importing share files (deleted when done).
- `trellis-runtime\`: the TRELLIS runtime and models (the default destination).

## Changing the locations

### The data folder

You can change the location of the data folder by adding `--data-dir` and a folder when you start `GDSkinStudio.exe`.

```
GDSkinStudio.exe --data-dir C:\GDSkinData
```

- A short path with only ASCII letters, digits and symbols is recommended. "Data and temporary folder paths" in "Environment check" on the "Settings" screen tells you about paths that are too long and similar problems.
- You need to specify it every time you start the app. To add it every time, create a shortcut to `GDSkinStudio.exe` and, in the shortcut's properties, add a space and `--data-dir C:\GDSkinData` to the end of "Target".
- You can also specify it with the `GDSKIN_HOME` environment variable. If both are given, `--data-dir` is used; if neither is given, the default `%LOCALAPPDATA%\GrimDawnSkinMOD` is used.
- The folder you specify is used as a separate data folder. The equipment, settings (including the display language), and install records in the previous data folder are not visible from it (they stay in the previous location). The TRELLIS runtime inside the previous data folder is not used automatically either (to use it, specify its location in "TRELLIS runtime" on the "Settings" screen). The first time you start the app with the new location, the "Setup" screen opens again.
- Because the install record is in the data folder, you can remove a MOD with "Uninstall…" only while the app is running with the same data folder that was used to install it.
- Only one instance of the app can run with the same data folder.
- If the folder you specify cannot be created or written to, "Cannot use the data folder" appears and the app exits.

### The TRELLIS runtime

- You can choose the destination in "Destination (TRELLIS runtime and models)" in "2. Components and consent" of the setup. Specify a dedicated empty folder with a path of 80 characters or fewer that contains no characters other than ASCII letters, digits, and symbols (such as Japanese or full-width characters; see [The "Setup" screen](#setup)).
- The destination is placed outside the app folder, so it is kept when the app is updated.
- If "TRELLIS runtime" on the "Settings" screen is blank, the app finds the runtime automatically, for example in `trellis-runtime` in the data folder.

## Usage and cleanup

You can check the size of each item in the data folder in "Data usage and cleanup" on the "Settings" screen. The TRELLIS runtime in the data folder is not counted (it is handled in "TRELLIS runtime management"). There you can delete old builds, old install backups, and more after checking what will be deleted and what will be kept. Items are deleted only when you click, and deleted items cannot be restored (see [The "Settings" screen](#settings)).

!> `install\` contains the backups used to remove the MOD and restore the original files. Do not delete the data folder while a MOD is installed in the game.

> When the app is started from inside Claude or another app, the data folder may be split off into a different location (a virtualized folder), and the equipment you made may not appear in the app when you start it normally. Start the app normally, for example from File Explorer (see [Troubleshooting](#troubleshooting)).

# Uninstalling the app {#uninstall}

The app does not use an installer and registers nothing in the registry or the Start menu. To uninstall it, you remove the MOD from the game and then delete the folders.

!> Always remove the MOD before you delete the data folder. If the install replaced files of another MOD, the original files exist only in `install\backups` in the data folder, and deleting the data folder makes it impossible to restore them.

> Before you remove the MOD, it is recommended to copy the save folder (`Documents\My Games\Grim Dawn\save`; if you use Steam Cloud, also `Steam\userdata\<number>\219990\remote\save`) as a whole to another location.

## Steps

1. Exit Grim Dawn, then click "Uninstall…" on the "MOD" screen of the app to remove the MOD from the game. The mods folder returns to its state before the first install (see [Returning the game to its state before the MOD](#mod-restore)).
2. If you do not use other `/basemods` MODs, remove `/basemods` from the Steam launch options (in the Steam library, right-click Grim Dawn → "Properties" → "General" → "Launch Options"). The app never changes the launch options, so remove it yourself.
3. (Optional) To remove the TRELLIS runtime, use "Remove the whole TRELLIS runtime…" in "TRELLIS runtime management (re-verify, reinstall, remove)" on the "Settings" screen; it removes the runtime and models the app downloaded (about 13 GB, or about 19 GB with the text → 3D items). Only destinations created by the app's setup are covered, and other files you placed there are kept.
4. Check the locations to delete. The data folder is shown in "Data folder" on the "Settings" screen ("Open" opens it). The location of the TRELLIS runtime is shown after "Destination:" under "Setup (installed items and licenses)" on the same screen.
5. Close the app (the backend also exits).
6. Delete the extracted folder (the folder that contains `GDSkinStudio.exe`) as a whole.
7. (Optional) To remove your data as well, delete the data folder (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`). This removes your equipment, MOD builds, install records and backups, settings, logs, and the TRELLIS runtime and models in the default location.
8. (Optional) If the TRELLIS runtime is outside the data folder (a location you chose during the setup, or `%SystemDrive%\GDSkinStudio\trellis-runtime`) and you did not remove it in step 3, delete that folder as well. If a `tmp` folder remains in `%SystemDrive%\GDSkinStudio`, you can delete it too.

## What remains after uninstalling

- In step 1, Grim Dawn's mods folder returns to its state before the first install. If there was an old AI Skin MOD with no record in this app (for example, a prototype) before the first install, the folder returns to that MOD, and the game loads it (the "MOD" screen and the confirmation window show this).
- If you did not remove it in step 2, `/basemods` remains in the Steam launch options.
- The app does not change your game saves. How illusions already in your in-game collection are shown after uninstalling has not been confirmed. What happens to this app's consumables and collected illusions when you start the game without `/basemods` has not been tested either.
- Share files you exported (`.gdskin`, `.gdskinmod`) and MODs you exported with "Export to folder…" remain where you saved them.
- The Microsoft Visual C++ Redistributable and the WebView2 Runtime, if you installed them yourself, remain (they are not part of this app).

## If the app cannot remove the MOD

If there is no install record, for example because you deleted the data folder first or installed with a data folder in another location (such as when the app was started from inside Claude or another app), "Install status" on the "MOD" screen shows "Not installed by this app", and "Uninstall…" cannot be clicked (if you can still click it, it stops with "No MOD has been installed by this app (the mods folder was not changed)."). The files in the mods folder remain, so do the following.

1. If you do not use other `/basemods` MODs, remove `/basemods` from the Steam launch options. This alone stops the game from loading the files directly in the mods folder.
2. To delete the files as well, exit Grim Dawn and check the "Owner" of each file in the "mods folder status" table on the "MOD" screen of the app.
3. Delete only the `database.arz`, `Items.arc`, and `Text_XX.arc` files directly in the mods folder that are shown as "Installed by this app" or "AI Skin MOD with no record" (`Text_CS.arc` to `Text_ZH.arc` for 13 languages; MODs made with earlier versions have only `Text_EN.arc` and possibly `Text_JA.arc`). "Open mods folder" opens the mods folder.
4. Do not delete files shown as "Another MOD or unknown file", or folders such as `mods\survivalmode` (the Crucible DLC).

# Troubleshooting {#troubleshooting}

Look for the heading closest to your symptom. The advice in the error window, its "Details (technical information)", and the "Log" screen on the left often show the cause. If the problem remains, see "Reporting a problem" at the end of this section.

## Starting the app

### "Windows protected your PC" appears when you start the app

- The app's EXE is not code-signed, so a Microsoft Defender SmartScreen window may appear. Click "More info" in the text of the window, then click "Run anyway", which appears at the bottom. Check the SHA-256 of the downloaded ZIP first ([Installing and updating the app](#install)).
- On a PC with Windows 11 "Smart App Control" turned on, the app may be blocked as an unsigned app, and no "Run anyway" button appears. In that case, this app cannot be used.
- Antivirus software may block (quarantine) `GDSkinStudio.exe` or `python\python.exe`. If you extracted them from a ZIP whose SHA-256 matches the one on the download page, consider whether to add the extracted folder to that software's exceptions (exclusions).

### "The app is already running" appears

- Only one instance of the app can run with the same data folder. Use the window that is already open.
- If the previous app is still closing, wait a moment and start it again.

### "Cannot use the data folder" appears at startup and the app exits

- The data folder (the location given with `--data-dir` or the `GDSKIN_HOME` environment variable, or `%LOCALAPPDATA%\GrimDawnSkinMOD` if neither is given), or the `logs` folder inside it, cannot be created or written to, or the path is malformed.
- Check the path you specified, the drive (for an external drive, that it is connected), and the access permissions, then start the app again.

### "Backend: stopped" or "Backend: cannot start" appears at the bottom left

- The backend (the process that runs generation, conversion, and MOD builds) has stopped or could not start. Click "Restart backend" in the bar at the top of the window.
- If this keeps happening, check that you extracted the ZIP before starting the app (it does not work from inside the ZIP), that you did not delete files in the extracted folder (such as the `python` folder), and that antivirus software is not blocking `python\python.exe`.
- If "Could not start Python (python.exe) for running the backend" or "Python 3.11 for running the backend was not found" appears, check "Backend Python (automatic if blank)" in Settings. When it is blank, the Python bundled with the app (`python\python.exe` in the extracted folder) is used. After fixing it, click "Save settings", then click "Restart backend".
- If "Could not start because another backend is running with the same data folder" appears, the previous app may still be closing. Wait a moment, then click "Restart backend".
- If the backend stopped after it had started, the end of its output at that time is kept in the ERROR lines on the "Log" screen and in `app-<date>.log`.

### "Cannot connect to the backend", "The backend is starting", or "The backend did not respond" appears

- For "The backend is starting", wait until the status at the bottom left shows "Backend: connected", then try again.
- For "Cannot connect to the backend", click "Restart backend" in the bar at the top of the window. If the message says "Restart the app", restart the app.
- "The backend did not respond" means that the backend did not respond within 120 seconds. The task may still be running in the backend, so wait a while, check the result (the output folder, the list, and so on), and try again if needed. If this keeps happening, check the log and restart the app.

### "Data and temporary folder paths" shows a warning in "Environment check" in Settings

- If the data folder path is too long, the paths of files created in it exceed the Windows limit (260 characters), and saving or building the MOD may fail. Specify a short folder (for example `C:\GDSkinData`) with the `--data-dir` option of `GDSkinStudio.exe` ([Where the app keeps its data](#files)). When you change the data folder, the equipment, install records, and settings in the previous data folder are not used.
- If the temporary folder path is too long, set the `TEMP` and `TMP` environment variables to a short folder.
- If there is a warning about the TRELLIS runtime path and 3D generation fails, download the runtime again in Setup to a short folder whose path uses only ASCII letters, digits, and symbols.

### Equipment made while the app was started from inside Claude or another app does not appear when you start the app normally

- When the app is started from inside some apps (MSIX-packaged apps), the data folder may be split off into a different location (that app's virtualized folder). New equipment, MODs, and install records are saved in that virtualized folder and cannot be seen by the app when it is started normally ("Data folder (how the app was started)" in the environment check and the Library show a warning).
- For everyday use, start the app from the desktop or File Explorer.
- Equipment in the virtualized folder can be imported with "Import…" in the Library of the app started normally. The internal IDs are kept, so illusions already collected in the game should keep working (not confirmed in the game). After importing, build and install the MOD.
- A MOD installed while the app was started that way cannot be removed with "Uninstall…" in the app started normally ([Returning the game to its state before the MOD](#mod-restore)).

### Some text is still in Japanese on the English screens

- A change of display language takes effect after you restart the app (until then, the previous language is shown).
- Log lines written before the switch stay in the language they were written in. Text you typed (such as names) is shown as you typed it.
- Error messages from Windows in the error "Details (technical information)" are in the display language of Windows.
- If you find Japanese text anywhere else, please let us know.

## Setup and downloads

### The download fails or stops partway

- When the connection drops, the app retries automatically several times and resumes partially downloaded files. When a connection is cut while starting encryption (the TLS handshake), the app automatically reconnects over the other kind of address, IPv6 or IPv4 (connections through a proxy are excluded). Retries and switches are shown in "Details" in "3. Download" and in the log.
- If the download still stops, click "Retry (continue)". Files already downloaded are verified and reused, and partially downloaded files are resumed. If it keeps failing, check the cause on the "Log" screen.
- The download fails if connections to the following are blocked, for example on a company or school network: github.com (including the redirect targets codeload.github.com and release-assets.githubusercontent.com), huggingface.co (including the delivery servers it redirects to, such as us.aws.cdn.hf.co), releases.astral.sh, download.pytorch.org, pypi.org, files.pythonhosted.org, dl.fbaipublicfiles.com, and nvidia-kaolin.s3.us-east-2.amazonaws.com.
- If "The source did not return the file" (with an HTTP status number) appears, the file at the source may have been moved or removed, or a proxy or filter may be blocking it. If it keeps happening, check for a new version of the app.
- If "Could not write to a file being downloaded" appears, another app or antivirus software may have the file open. Wait a moment, then click "Retry (continue)".
- If you closed the app during the download, you can continue the download in Setup after you start the app again.

### "Cannot verify a secure connection" appears

- The download was stopped because the source server's certificate could not be verified (in this case, the download is not retried automatically).
- HTTPS (encrypted traffic) scanning by antivirus software or a corporate proxy may be replacing the certificate. Check that the certificate is registered in Windows, or exclude this app from the scanning, then click "Retry (continue)".

### "A downloaded file did not match the record (SHA-256)" or "A downloaded archive (ZIP) is corrupted…" appears

- The file at the source may have changed, or it may have been corrupted during transfer. Wait a while, then click "Retry (continue)" to download it again.
- If it still does not match after several tries, the file at the source may have changed, so check for a new version of the app.

### "Not enough free space" appears

- There is not enough free space on the destination drive. A new download needs about 18 GB of free space for the required items (about 13 GB after the download). Text → 3D needs about 6 GB more.
- Choose a folder on a drive with more free space in "Destination (TRELLIS runtime and models)" in "2. Components and consent", or free up space, then retry.
- "Reinstall Python and packages…" in Settings also needs extra space temporarily during the reinstall. Free up space, for example by deleting unneeded files, then run it again.

### An error appears when you specify the destination

- Use a dedicated empty folder as the destination (for example `D:\trellis-runtime`). The following cannot be used:
  - Paths longer than 80 characters, or paths with characters other than ASCII letters, digits, and symbols (such as Japanese or full-width characters)
  - Paths that do not start with a drive, or the root of a drive
  - Folders inside the app folder, the Windows folder, or Program Files
  - The Grim Dawn folder, folders inside it, and folders that contain it
  - The data folder itself and folders that contain it
  - Folders that cannot be written to, and places where a file has the same name as the folder
- To return to the default destination, click "Reset to default".

### "The Visual C++ Redistributable is required" appears (self-test after the download)

- The Microsoft Visual C++ Redistributable (x64) on this PC is old or not installed. An old version is often installed together with Grim Dawn, and with the old version, the self-test after the download fails.
- Install the latest version (`https://aka.ms/vc14/vc_redist.x64.exe`; administrator rights are required), then click "Retry (continue)". Files already downloaded are reused.
- If "1. Check environment" reports it as old or missing, install it before the self-test after the download. GLB conversion and MOD builds are not affected.

### "Setup failed" appears

- Creating the TRELLIS runtime, installing packages, or the self-test failed. Check the "Details (technical information)" and the log. "Retry (continue)" restarts from the item that failed.
- If the self-test failed, "Retry (continue)" does not reinstall PyTorch and the packages that are already installed. If components are broken, you can reinstall them with "Reinstall Python and packages…" in "TRELLIS runtime management" in Settings (PyTorch, about 3.5 GB, is downloaded again).
- If the destination was not created by the app's setup (when you use an existing TRELLIS runtime), delete the `venv` folder inside it, then click "Retry (continue)".

### On a PC without a usable GPU, you cannot go past "3. Download" with "Next"

- Without the TRELLIS runtime, you cannot go from "3. Download" to "4. Reference meshes" and "5. Launch options" (a limitation of this app).
- If you do not use 3D generation, close the setup with "Do it later". Prepare the reference meshes with "Prepare from the game" on the "Settings" screen, and see the steps for setting the launch option `/basemods` with "How to set /basemods" on the "MOD" screen ([The "Setup" screen](#setup)).

### A failure on the "Settings" screen shows a hint about "Retry (continue)"

- When "Reinstall Python and packages…" or "Re-verify models…" in "TRELLIS runtime management" fails to download or check something, the hint in the error window also points to "Retry (continue)" on the Setup screen. The "Settings" screen does not have this button (a limitation of this app).
- Fix the cause, then click the same button on the "Settings" screen again.

### You are asked again to accept the licenses before a download or repair

- When the license or notice text shown has changed, you need to accept again (acceptance is recorded for the exact content shown). Check the licenses in "2. Components and consent" of "Setup" on the left and accept them again. Files already downloaded are verified and reused.
- Until you accept again, the setup download and "Reinstall Python and packages…" in Settings stop and ask for acceptance, and "Re-verify models…" does not re-download files that do not match (the result says "Download them again from the Setup page"). Generation with the runtime you have already downloaded keeps working.

## Generation

### "No supported GPU found", "The GPU driver is too old", or "This GPU does not support 3D generation" appears

- 3D generation requires an NVIDIA GPU (compute capability 6.0 or higher) and driver R570 or later. Update the driver from NVIDIA's official website or the NVIDIA App.
- The app has been tested only with a GeForce RTX 5090 Laptop GPU (Windows 11). Generation on GPUs with compute capability below 7.0 has not been tested.
- If you set the `CUDA_VISIBLE_DEVICES` environment variable, check that it specifies a GPU in this PC; after fixing it, restart the app.
- Even on a PC without a usable GPU, conversion with the "GLB file (advanced)" input and building and installing MODs are available (the setup cannot download the 3D generation components).

### "Not enough GPU memory" appears, or generation fails for lack of memory

- 3D generation requires 12 GB or more of GPU memory (16 GB or more recommended). Generation on GPUs with at least 12 GB but less than 16 GB has not been tested (the TRELLIS README requires 16 GB or more). On such GPUs, the setup shows a warning before the download starts and lets you choose whether to continue.
- Close games and other apps that use the GPU, then try again.
- Turning on "Low VRAM mode" in "Generation settings" → "Advanced settings" lowers peak GPU memory use by about 3.5 to 4.5 GB (measured on a 24 GB GPU; each candidate takes about 2 seconds longer). Whether it works on 12 to 16 GB GPUs has not been confirmed.
- Lowering the "Texture resolution" or the number of candidates does not change memory use much.

### "Not enough PC memory" appears

- The PC ran out of memory (RAM) or virtual memory (paging file) while loading the AI models or generating the 3D model. The model files are not damaged.
- Close other apps, then generate again. If this keeps happening, set the Windows virtual memory (paging file) to "System managed size" or make it larger, and free up space on drive C.

### "TRELLIS models are missing", "A file needed for generation is missing", or "Required libraries not found" appears

- Files of the TRELLIS runtime or the models used for 3D generation are missing, or were only partly downloaded. Files are never downloaded during generation, so generation was stopped.
- Run "Setup" on the left. Missing files are downloaded, and files already downloaded are verified and reused. "Environment check" in Settings also shows what is missing.
- If files may be corrupted, use "Re-verify models…" in "TRELLIS runtime management" in Settings to check every file (only the files that do not match are downloaded again).
- If "Block network access" is on in the advanced generation settings and "Generation failed because network access was blocked" appears, a required file may also be missing or corrupted. Verify the downloaded files with "Setup" on the left.

### "Could not load the models" appears

- Check the "Details (technical information)" and the log. This can also happen when GPU memory is low.
- If the model files may be corrupted, use "Re-verify models…" in "TRELLIS runtime management" in Settings; if Python or the packages may be damaged, use "Reinstall Python and packages…". Both cover only destinations created by the app's setup.

### "Reference meshes are missing", "The reference meshes are corrupted", or "Cannot open the reference meshes" appears

- Reference meshes are the game's body meshes used for the conversion. They are extracted from your Grim Dawn and saved in the data folder (the game folder is not changed).
- If they are missing or corrupted, prepare (extract) them again with "Prepare reference meshes" in the error window, "4. Reference meshes" in the setup, or "Prepare from the game" in Settings. If Grim Dawn is not found, enter the Grim Dawn folder (the folder that contains `database\database.arz`) in "Grim Dawn install folder" in Settings or find it with "Detect", then click "Save settings".
- If they cannot be opened (another app is using them, or you do not have permission to read them), close or pause antivirus, backup, or sync software (OneDrive, etc.), or check that you have permission to read the `refs` folder in the data folder, then try again. "Prepare from the game" does not fix this.

### "3D generation failed" or "The AI did not generate an object" appears

- Try another seed, or another image or text (you can change the seed with "Random seed" in "Generation settings").
- If this keeps happening, check the "Details (technical information)" and the log.

### Part of the character disappears or the background remains in an image with a background

- Check the image used for generation first with "Check background removal". If it does not work well, use a PNG with a transparent background.
- You can also compare the "Background removal" setting (background removal in the app with post-processing) in "Generation settings" → "Advanced settings" using "Check background removal". The effect depends on the image.
- When you make a transparent PNG yourself, make the background fully transparent and the character opaque. Semi-transparent parts (light, hair, shadows, etc.) disappear or become dark.

### The background becomes part of the 3D model (a board or a stand)

- The background is not removed from an image that is almost entirely opaque, such as a screenshot with only transparent corners ("Points to check" says "almost all of it was opaque, so it was generated without removing the background").
- In the image field, turn on "Force background removal" in "Advanced settings for this image" and generate again, use a PNG with a transparent background, or save the image again as a JPEG.

### The image shows "no transparency (cannot be used as is because the background removal model is missing)"

- To use an image with a background, the background removal model (u2net) is required. Download it with "Setup" on the left (it is one of the required items).
- To use the image right away, use a PNG whose background was made transparent, for example with the "remove background" function of an image editor.
- If "Cannot load the background removal model" appears, also run "Setup" on the left (a file with the wrong size is downloaded again). If that does not help, use "Re-verify models…" in "TRELLIS runtime management" in Settings to check every file.

### An error appears when you select an image

- The supported formats are PNG, JPEG, WebP, and BMP. HEIC images, such as iPhone photos, cannot be used; convert them to JPEG or PNG before selecting them.
- Image files can be up to 40 MB, and images can be 64 to 16,384 pixels per side (up to 160 million pixels in total).
- Images with almost no opaque area (character), and images in which no character is left after removing the background, cannot be used. Use another image in which the character is clearly visible.

### Bodies made from text come out white or gray (no color)

- Without colors in the text, the body tends to come out white or gray. Also write the colors of the hair, clothes, shoes, and so on in English (example: long black hair, a red ribbon, a navy blue skirt).
- If the selected body is almost only white, gray, or black and another candidate has color, "Points to check" shows a warning. To choose again, click "Use this candidate" for a candidate marked "Has color" on the "Candidates" tab of the preview (the marks and the warning appear when the body is chosen from candidates with "Pose selection").
- If no candidate has color, write the colors in English in the text and generate again.

### The end of the text is not used, or a warning about the text appears

- The generation AI reads up to 77 tokens, including the two start and end markers (roughly 50 to 60 English words). Anything beyond that is not used (the automatically added "no weapons" instruction is kept). An estimate of the token count is shown under the text field, together with the part that is not used when the text is too long.
- The generation AI expects English text. Words with non-English characters and full-width letters, digits, or symbols (for example, the full-width forms of letters, digits, parentheses, and the tilde) cause a warning, so change them to English and half-width characters.

### Generation from text does not work, or "You need to accept the notice" appears

- Generation from text requires the optional setup items (TRELLIS-text-xlarge and CLIP) and accepting the notice from the CLIP model card. If you have not downloaded them, select the optional items in "Setup" on the left and download them.
- Accept the notice with "Read the notice…" under the text field, or with "Read the notice…" in "CLIP notice (generation from text)" in Settings. Generation from images works without accepting it.
- If the CLIP notice changes, you are asked to accept it again before generating from text, even if you accepted an earlier version.

### "Points to check" appears after generation

- Generation and conversion were not stopped. Check the preview before you decide whether to save.
- If "The model may contain an object that looks like a weapon" or "The pose fitting is weak" appears, try "Regenerate with another seed".
- If "Some parts were stretched a lot by the conversion" appears, check in the "Male (converted)" and "Female (converted)" previews that the feet and shins are not torn or stretched. If they are broken, regenerate with another seed or use an image of a standing pose with the arms and legs slightly apart.
- If a note says that the subject touches the edge of the image, that it is small, or that there are several separate objects, an image showing one character's whole body at a large size tends to give equipment that fits the body better and has cleaner details.

## Preview

### The 3D preview is not shown or stops

- If "The 3D preview requires the Microsoft Edge WebView2 Runtime" appears: install the WebView2 Runtime, then restart the app. It is included with Windows 11. If it is missing, install it with the "Evergreen Bootstrapper" on Microsoft's download page (`https://developer.microsoft.com/microsoft-edge/webview2/`).
- If "The 3D preview cannot be shown on this PC" appears: updating the graphics driver may help.
- If "Cannot open the 3D preview page" appears: some app files may be missing, so reinstall the app.
- If "The 3D preview stopped repeatedly, so automatic restarting was paused" appears: click a model button above the preview, or switch to another screen and come back, to open it again.
- In every case, you can check the conversion result on the "Conversion check image" tab of the preview.

### The front faces backwards

- If you see the back when you view "Male (converted)" or "Female (converted)" from "Front", click "Rotate 180° and reconvert" below the preview. The 3D generation is not repeated; only the conversion is redone.
- Turning on "Front marker" shows an arrow on the floor and the label "Front (+Z)" in the in-game front direction (+Z). If the face or chest side faces away from the arrow, rotate 180° and reconvert.
- For saved equipment, check "Rotate the front 180° and reconvert (when it looks backwards in the game or the preview)" in the confirmation window of "Reconvert" in the Library, and reconvert.

## Library and conversion

### "Reconvert" cannot be clicked, or a message says that the equipment cannot be reconverted

- Equipment with no saved generation result (3D model), such as equipment made with an older version or imported equipment, cannot be reconverted. Use "Regenerate" to start over from the 3D generation.
- The button also cannot be clicked during generation or while the app is not connected to the backend.

### "Model conversion failed" or "Rigging failed" appears

- Humanoid models standing with the arms slightly away from the body succeed most often. Regenerate with another seed, or try another image.
- Also check the "Details (technical information)" and the log.

### A GLB file made with another tool cannot be converted

- Use a GLB with Y up and the front facing +Z (the height is adjusted automatically).
- GLBs with mesh compression (Draco, meshopt) or with extensions this app cannot read cannot be loaded. Export them again from the tool that made them without compression or those extensions (use PNG or JPEG for textures).
- For a GLB that references texture images or .bin as external files, only files referenced by a relative path from the GLB's folder are loaded. Put the referenced files in the same folder as the GLB and export it again, or use a GLB with the textures embedded.
- If "Some vertices are extremely far from the body" appears, the GLB has broken or extra parts (such as small fragments or stray points) extremely far from the body (more than 100 times the body height). Remove them in the tool that made the GLB, then export it again. Parts that are closer but still far away from the body do not stop the conversion; they are reported in "Points to check" (the parts remain in the equipment).
- If the GLB is too large (the number of vertices or nodes, or the total pixel count of the textures, exceeds the limit), reduce the polygon count or the number and size of the textures, then export it again.

### "Checking the compression of the texture (.tex) failed" appears

- For textures with fine patterns, choosing a smaller "MOD textures" size (such as 1024) in "Generation settings" → "Advanced settings" and then clicking "Start generation" again may help (reconverting a draft or saved equipment keeps the size used when it was made).

### "About the English name" appears when you save

- The English name contains characters that probably will not be shown in the game. The game's fonts in English only have ASCII characters, so Japanese text and characters such as é and “ ” will probably not be shown. Plain ASCII letters, digits, and symbols are recommended.
- The equipment has been saved. To change the name, fix it in "Edit names" in the Library and click "Save names". To update the game, rebuild and install the MOD.

## Building and installing the MOD

### "MOD build failed" or "The MOD build stopped partway" appears

- The mods folder was not changed. The "Details (technical information)" in the error window shows the cause (such as the output of the game's bundled tools or the list of problems found by the validation).
- If "There is no equipment to put into the MOD" appears, first create equipment on the "Generate" screen and save it with "Save as equipment".

### "Grim Dawn not found" or "The game's bundled tool is missing" appears, or Ashes of Malmouth is reported as missing

- "Grim Dawn not found" means that the Grim Dawn install folder (or the MOD install target) was not found, or that it is not a Grim Dawn folder. Enter the Grim Dawn folder (the folder that contains `database\database.arz`) in "Grim Dawn install folder" in Settings, or find it with "Detect", then click "Save settings".
- If "The game's bundled tool is missing" appears, the Grim Dawn folder does not contain ArchiveTool.exe, which is used to build the MOD, or TextureCompiler.exe, which makes textures (.tex) during conversion. You can restore them in the Steam library: Grim Dawn's Properties → "Installed Files" → "Verify integrity of game files".
- Building the MOD requires the expansion "Ashes of Malmouth" (the MOD template uses Ashes of Malmouth data).
- "Environment check" in Settings shows whether ArchiveTool.exe, TextureCompiler.exe, and the expansion Ashes of Malmouth are present. Also check that "Grim Dawn install folder" in Settings is the Grim Dawn folder you play with.

### "Grim Dawn is running" appears and you cannot install or uninstall

- The MOD cannot be installed or uninstalled while the game is running (the files are in use and changes would not take effect). Exit Grim Dawn completely, then try again.
- If "Could not check whether Grim Dawn is running" appears, make sure the game has exited completely before continuing.

### "Another MOD's files are present" appears

- The mods folder contains files from another MOD (not installed by this app) or files changed since this app installed them. If you continue, those files are backed up to the data folder and then replaced. Uninstalling restores the original files.
- Only one `mods\database.arz` is read, so after the replacement, that MOD no longer works correctly. To keep using that MOD, stop the installation.

### "Installation stopped" appears

- The mods folder was not changed. The reason is shown in "Details (technical information)".
- If there are read-only files, open the file's properties in File Explorer, clear "Read-only", then try again.
- If a build file is missing or does not match the record, rebuild with "Build MOD" before installing (antivirus software may have quarantined the file).
- If the backup of the state before installation was not found and you deleted or moved backups in the data folder, put them back where they were and try again. Without the backup, the state before installation cannot be restored.
- If "The install record is corrupted" appears, fix the install record (`install\state.json` in the data folder) with a text editor such as Notepad, or rename it to move it aside. Then the app starts without the record, but uninstalling can no longer return to the state before the first install.

### Installing or uninstalling failed

- If writing fails, the mods folder is returned to its state before the operation. Close Grim Dawn and any program that has files in the mods folder open, then try again. If antivirus software is scanning the files, wait a moment and try again.
- If you do not have permission to write, check that you can write to the Grim Dawn folder (that it is not a location requiring administrator rights). If there is not enough free space, free up space on the drive of the Grim Dawn folder and on the drive of the data folder.
- If the message says that "some files could not be restored", close Grim Dawn and any program that has the files open, then run the same operation (install, uninstall, or restore) again. The files from before the operation are backed up in `install\backups\<ID>` in the data folder.
- If "Could not save the install record" appears, the mods folder has been changed, but the install record is out of date. Close any program that is using the files, then run the same operation again (if you are told that files were changed, continue). You can check the current state in the "mods folder status" table on the "MOD" screen.
- If "Done (with warnings)" appears, read the warnings. "Could not update the install status in the equipment library" means that the changes to the mods folder are complete, but marks such as "Installed" in the Library may be out of date (the log has the reason).

### "The install target (Grim Dawn folder) was not found" appears

- The install status cannot be checked, and you cannot install or uninstall. Check "Grim Dawn install folder" in Settings (or "MOD install target (for testing; normally blank)" if you use a test install target).
- If it is on an external drive, connect the drive, then click "Refresh" on the "MOD" screen.

### A warning appears on the "MOD" screen after a game update (patch)

- The MOD contains a copy of the Quartermaster's stock (official records), so when a game update changes the stock, the MOD overwrites it with the old contents.
- As the warning says, rebuild with "Rebuild the MOD" ("Build MOD"), then install again with "Install the latest build" ("Install to Grim Dawn…"). If the latest build already matches the current game, installing it again is enough.
- The "Game version" row shows the Steam build numbers (when the MOD was built and now) and the result of the comparison. If Steam is waiting to update the game, click "Refresh" after the update finishes to check again.
- If you use a received MOD, ask the person who made it to rebuild it with the current game and share it again, then receive and install it again. If you replace it with your own MOD, your verification code will no longer match theirs.

### An old AI Skin MOD remains after uninstalling

- Uninstalling returns to "the state before the first install". If the mods folder contained an AI Skin MOD with no record in this app (for example, a prototype) at that time, the folder returns to that MOD, and the game loads it (the "MOD" screen and the confirmation window show this).
- If you do not need it, exit the game, open the folder with "Open mods folder", and move those files somewhere else.

### "Uninstall…" cannot be clicked, or "No MOD has been installed by this app" appears

- This data folder has no install record (for example, you deleted the data folder, or you installed with a data folder in another location or in another app's virtualized folder). The mods folder was not changed.
- To remove the files by hand, see [Returning the game to its state before the MOD](#mod-restore).

### "A task that cannot be interrupted is running" appears when you close the app

- Stopping a MOD install, uninstall, or restore, or an equipment import, partway would leave the mods folder or the Library in an incomplete state. Click "Wait and close", and the app closes automatically when the task is done.
- If the task has not finished after 2 minutes, "The task has not finished yet" appears, and you can choose "Keep waiting" or "Close anyway". If you close anyway and something is left in an incomplete state, install again from the "MOD" screen the next time you start the app.
- If you close the app during generation, a MOD build, an export, a setup download, or a similar task, "Tasks are running" appears, and "Cancel and close" cancels the tasks and closes the app. A setup download can be continued after you start the app again.

## In the game

### The illusion does not appear, or it is not in the Quartermaster's stock

- Check that the Steam launch options include `/basemods`. Without it, the MOD is not loaded. Start Grim Dawn from Steam ([Installing a MOD and using it in the game](#mod-install)).
- Check in "Install status" on the "MOD" screen that the MOD is installed. Also check that the "Install target" is the Grim Dawn folder you play with (and not a "Test install target").
- Only one `mods\database.arz` is read, so this MOD cannot be used together with other main-campaign MODs. Also check that the "mods folder status" table has no "Other file (loaded)".
- This MOD is loaded in the main campaign. The stock of the Quartermaster (Ellis in Devil's Crossing) appears at Survivors reputation of Friendly or higher. Buy "Illusion: <name>", close the shop, and right-click it to add it to your collection (right-clicking it while the shop is open sells it back).
- Illusions can only be applied to equipped slots. Equip something on the head, chest, shoulders, hands, legs, and feet, then apply the illusions at the Illusionist.

### /basemods is set, but the app reports that it is not set

- The app reads the settings of the user currently logged in to Steam (if that cannot be determined, the user who logged in last). It only reads them and never changes them.
- Right after you set the option while Steam is running, the setting is saved to a file only when you exit Steam, and then it can be checked. Exit Steam, then click "Check again" ("Refresh" on the "MOD" screen).
- To play as another Steam user on this PC, set the option for that user.

### The feet or shins are distorted or torn

- This is more likely at the feet and shoes of bodies standing with the legs together.
- For equipment converted with an earlier version of the app, select the equipment in the Library and click "Reconvert" to rebuild it with the current conversion without redoing the 3D generation (the rigging of the feet and so on is renewed, and illusions already obtained in the game should keep working as the same items; not confirmed in the game). When it is done, click "Rebuild the MOD", then install it again with "Install to Grim Dawn…".
- If they are still distorted, try another seed with "Regenerate", or use an image of a standing pose with the arms and legs slightly apart.

### The equipment faces backwards

- Select the equipment in the Library, check "Rotate the front 180° and reconvert (when it looks backwards in the game or the preview)" in the confirmation window of "Reconvert", and reconvert. Then rebuild and install the MOD.

### Saved equipment or changes do not show up in the game

- Saving, renaming, deleting, or reconverting equipment alone does not change the game. Click "Build MOD" on the "MOD" screen, then install it with "Install to Grim Dawn…".
- If the "MOD" screen says "The Library has changed" or that the latest build is not installed yet, the changes are not in the game yet.

### Item names or descriptions are not shown correctly

- The game's fonts in English only have ASCII characters, so Japanese text and characters such as é and “ ” in English names and English descriptions will probably not be shown. Change them to plain ASCII letters, digits, and symbols with "Edit names" or "Consumable text…" in the Library.
- The MOD includes name files (`Text_XX.arc`) for 13 languages (Japanese names for the game in Japanese, English names for the other languages). The game was confirmed to read them when it is set to English or Japanese; whether it reads them with other languages has not been confirmed.
- If a Japanese description is cut off before the end, it is because the game wraps descriptions only at half-width spaces. Put a half-width space between phrases with "Consumable text…". The default Japanese descriptions in MODs made with earlier versions of the app may be cut off, so build the MOD again with this version and install it.
- After fixing them, rebuild and install the MOD.

## Sharing

### Your verification code does not match the other person's

- Check which files differ in "Verification details (code for each file, comparison with the other person's code)" on the "MOD" screen. You can enter the other person's verification code in "Their code" and click "Compare".
- If `database.arz` is the same and only the appearance (`Items.arc`) and names (`Text_XX.arc`) differ, you are expected to be able to connect, but each of you sees different appearances or names for the equipment.
- To give everyone the same MOD, one person exports it with "Export the MOD to a share file…", and the others install it with "Import and install a share file…". How it shows in multiplayer has not been confirmed in the game.

### "Cannot read the share file" or "This file cannot be imported" appears

- Nothing was imported. The file may be corrupted or may have been modified along the way (including a download that was cut off). Get the file from the creator again, or ask them to export it again.
- If "The file was made with a newer version of GD Skin Studio" appears, update the app, then import it.
- Import a MOD share file (`.gdskinmod`) with "Import and install a share file…" on the "MOD" screen, and an equipment package (`.gdskin`) with "Import file…" in the Library.
- Do not use files received from people you do not know.

### "This MOD cannot be installed" appears for a received MOD

- The MOD in the share file contains things GD Skin Studio never creates (such as changes to official data), so it is not installed (the mods folder was not changed). Ask the other person to rebuild the MOD with GD Skin Studio and export it again.
- "Points to check" in the confirmation window are caused by a different game version or by changes made by the creator. They alone do not stop the installation, but check the contents before installing.

### After installing a received MOD, your own equipment cannot be used in the game

- Only one `mods\database.arz` is read, so equipment that is not in the received MOD cannot be used in the game.
- To use both, exchange equipment packages (`.gdskin`) and build a MOD with the same set of equipment.
- "Remove the received MOD and restore" returns to the state before the received MOD was installed.

## Reporting a problem

### Viewing and collecting the logs

- The "Log" screen on the left shows a record of operations and processing. The "Activity" tab shows the operations on the screens and the progress of tasks, and the "Backend log file" tab shows the end of the backend's log ("Copy" copies it). Turn on "Show only warnings and errors" to show only warnings and errors on the "Activity" tab.
- "Open log folder" opens the log folder. Its location is shown to the right of the button (by default `%LOCALAPPDATA%\GrimDawnSkinMOD\logs`). The error window has the same button.
- The log folder contains `app-<date>.log` (the app's record), `backend-<date>.log` (the backend's record), and the `jobs` folder with the detailed output of each task. Logs older than 14 days are deleted automatically.
- The logs do not contain the text or images you enter, or your user folder name (your user folder is replaced with `%USERPROFILE%`).

### "Details" and the error code in the error window

- The error window shows what happened and what to do. Open "Details (technical information)" to see the technical information and the error code (you can select and copy them).

### Reporting a bug

- Bug reports and reports of whether the app worked in your environment are welcome (GitHub Issues). Including the following makes it easier to find the cause:
  - The GD Skin Studio version ("About" in Settings), and whether you use the distribution ZIP or a build from source
  - The Windows version (10 / 11, build number) and display language
  - The GPU name, GPU memory, and NVIDIA driver version (for 3D generation problems)
  - What you did and what happened (the steps, what you expected, and what actually happened)
  - The text of the error window and the error code, and the logs (the relevant parts of `app-<date>.log` and `backend-<date>.log`)
- Do not include personal information or anything protected by other people's rights in what you attach.
  - Check that the logs and screenshots do not show your Windows user name, email address, Steam ID, or similar.
  - Attach only input images that you may make public (do not attach images of other people's works or characters).
  - Do not attach Grim Dawn files (`.arz`, `.arc`, `.msh`, `.tex`, `.dbr`, etc.) or save data.
- For security problems, do not write the details in a public issue; first report only that there is a security problem.
