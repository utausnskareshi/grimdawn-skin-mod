# GD Skin Studio

English | [日本語](README.md)

A Windows tool that makes Grim Dawn MODs that "turn AI-made 3D models into illusion equipment".
It generates a 3D model from an image (or text), converts it for Grim Dawn's male and female characters,
builds a MOD that you can use as a head illusion, and installs it into the game folder.

- The images and text you enter are never sent outside this PC (3D generation also runs on your own GPU).
  The tool connects to the internet only when the setup downloads the 3D generation runtime and models (the first download, and
  re-verifying or reinstalling with "TRELLIS runtime management" in Settings). During 3D generation, missing files are not downloaded:
  generation stops with an error (default behavior). Turning on "Block network access" in the advanced generation settings refuses outgoing connections from the generation process (Python) (connections that native code opens by itself are not covered).
- This is an unofficial fan-made tool. It is not affiliated with Crate Entertainment (the developer of Grim Dawn), Microsoft (the developer of TRELLIS),
  or any other developer of the software and models this tool uses ([Disclaimer](#disclaimer)).

> The current version is **1.0.0** (the first release). It has been tested in only a few environments ([System requirements](#system-requirements)).
> Bug reports and reports of whether it worked in your environment are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) (Japanese).
>
> This README matches the current source in the repository. The changes listed under "未公開" (Unreleased) in [CHANGELOG.md](CHANGELOG.md) (Japanese)
> are not yet in the distribution ZIP published on Releases.
>
> The app starts in Japanese. To use it in English, see [Switching the screens to English](#switching-the-screens-to-english).

## Contents

1. [What it does](#what-it-does)
2. [System requirements](#system-requirements)
3. [Installation (from the distribution ZIP)](#installation-from-the-distribution-zip)
4. [First-run setup](#first-run-setup)
5. [Usage](#usage)
6. [Steam launch option /basemods](#steam-launch-option-basemods)
7. [Multiplayer (unverified)](#multiplayer-unverified)
8. [Troubleshooting](#troubleshooting)
9. [Uninstalling](#uninstalling)
10. [Where data is stored](#where-data-is-stored)
11. [For developers](#for-developers)
12. [License and third-party notices](#license-and-third-party-notices)
13. [Disclaimer](#disclaimer)

## What it does

- **Generate a 3D model from an image**: Creates a 3D model with Microsoft's [TRELLIS](https://github.com/microsoft/TRELLIS)
  (TRELLIS-image-large) from an image of one character showing the whole body. You can also add back and side views (up to 4 images).
  If the image has a background, it is removed automatically. If the subject is cut off at an edge or is small, or the image contains several objects, a warning is shown (generation is not stopped).
- **Generate a 3D model from text** (optional): Creates a model from English text with TRELLIS-text-xlarge (requires an additional download and acceptance of the CLIP notice).
  It also shows text templates and an estimate of the text length the generation AI reads (in tokens).
- **Convert for Grim Dawn**: Converts the generated model (or a GLB file made with another tool) into the game's full-body skinned meshes (`.msh`) and
  textures (`.tex`) for male and female characters. Pose and body fitting, vertex count adjustment and bone assignment are automatic.
- **3D preview**: You can rotate and inspect the generated model and the male and female conversion results. If the front faces backwards, you can rotate it 180° and reconvert.
  You can also pick a candidate (seed) other than the automatically chosen one and remake it.
- **Build and install the MOD**: Give your equipment a name (English and Japanese) and save it to the Library, then build one MOD with all your equipment and
  install it into Grim Dawn. The original files are backed up before installing, and uninstalling restores them.
  The MOD includes name files for the game's 13 languages (`Text_XX.arc`) (Japanese names for the game in Japanese; the Japanese names and descriptions were confirmed in the game; whether the game reads them with languages other than English and Japanese has not been confirmed).
- **Getting it in the game**: You get your equipment as an illusion from the Quartermaster in Devil's Crossing (Survivors reputation of Friendly or higher)
  and use it as a head illusion. Invisible (transparent) illusions for the chest, legs, hands, feet and shoulders are also included.
  An invisible medal illusion is also added (it should hide the badge on the chest; not confirmed in the game).
- **Sharing**: You can create files to exchange your equipment with other users (`.gdskin`) and MOD share files that hand over the MOD as is for multiplayer (`.gdskinmod`).
- **English screens** (optional): The default display language is Japanese. When you choose English in "Display language" in Settings, the app is shown in English,
  including messages from the backend and error details ([Advanced settings and additional features](#advanced-settings-and-additional-features)).

## System requirements

### Required

- Windows 10 / 11 (64-bit)
- Grim Dawn (Steam version) and the expansion "Ashes of Malmouth" (the MOD template uses Ashes of Malmouth data)
- Free disk space: about 270 MB for the app (the ZIP is about 127 MB) and about 13 GB for the 3D generation runtime and models
  (more is needed temporarily while downloading; for a new download, the setup asks for about 18 GB of free space and shows this on the setup screen;
  adding text → 3D takes about 6 GB more)
- An internet connection for the first-run setup (about 8.6 GB is downloaded)

### Needed for 3D generation (from images and text)

- An NVIDIA GPU (12 GB or more of GPU memory, 16 GB or more recommended, compute capability 6.0 or higher)
- NVIDIA driver R570 or later (one that supports CUDA 12.8)
- A recent version of the Microsoft Visual C++ Redistributable (x64). An old version is often installed together with Grim Dawn,
  and with the old version, the self-test after the download fails. If the environment check reports it as old or missing,
  install the latest version from [Microsoft's download link](https://aka.ms/vc14/vc_redist.x64.exe) (administrator rights are required).

> On a GPU with at least 12 GB but less than 16 GB of GPU memory, a warning is shown before the setup download starts, and you can choose whether to continue.
> The TRELLIS README requires 16 GB or more, and generation on GPUs of this size has not been tested.
> If generation fails for lack of memory, try "Low VRAM mode" in the advanced generation settings (on a 24 GB GPU, peak use was measured to drop from about 14 GB to about 10 GB;
> whether it works on 12 to 16 GB GPUs has not been confirmed).
>
> The tool has been tested on only one PC: a GeForce RTX 5090 Laptop GPU (24 GB) with Windows 11 (Japanese).
> It has not been tested on other GPUs (RTX 40 series or older, etc.), on Windows 10, or on Windows in languages other than Japanese.
> Even without a GPU, you can convert GLB files made with other tools and build and install MODs.
> However, converting a GLB with more than 25,000 vertices or with several materials or textures uses a component
> (for rebuilding the UVs) that relies on the Windows Visual C++ runtime, so the recent Visual C++ Redistributable above
> is required (what happens on a PC with only an old version has not been tested).

### Not needed

- You do not need the .NET runtime, Python, Visual Studio, the CUDA Toolkit or git
  (everything needed is either bundled or downloaded during the first-run setup).
  Administrator rights are not needed either (except to install or reinstall the Visual C++ Redistributable).
- The 3D preview uses the Microsoft Edge WebView2 Runtime. It is included with Windows 11.
  If it is missing, install it with the "Evergreen Bootstrapper" on the [WebView2 page](https://developer.microsoft.com/microsoft-edge/webview2/).

## Installation (from the distribution ZIP)

1. Download `GDSkinStudio-<version>-win-x64.zip` from [Releases](../../releases/latest).
2. (Recommended) Check that the ZIP is not damaged or tampered with: compare the SHA-256 shown on the Releases page with the value of your ZIP.
   Open PowerShell in the folder that contains the ZIP and run the following (if the 64 characters shown match, it is the same file).

   ```powershell
   Get-FileHash .\GDSkinStudio-1.0.0-win-x64.zip -Algorithm SHA256
   ```

   If they do not match, do not use it; download it again (the SHA-256 of each file after extraction is in `MANIFEST.txt`).
3. Extract the ZIP to a folder of your choice (for example `C:\Tools\GDSkinStudio`).
   - Avoid `C:\Program Files`, where writing is restricted.
   - A short folder path without full-width characters (such as Japanese) or non-English characters (such as é) is recommended.
   - Do not run the app from inside the ZIP; always extract it.
4. Start `GDSkinStudio.exe` in the extracted folder. Only one instance of the app can run with the same data folder.

It is likewise recommended that the path of the data folder (by default `%LOCALAPPDATA%\GrimDawnSkinMOD`) not contain full-width or similar characters.
If your Windows user name contains full-width characters, the default data folder path contains them too. A workaround for the game's bundled tools not handling such paths
is included, but it has not been tested on a real PC with a full-width user name. You can change the location of the data folder with
`GDSkinStudio.exe --data-dir <folder>` (a short path with only ASCII letters, digits and symbols is recommended; "Data and temporary folder paths" in "Environment check" in Settings
tells you about paths that are too long and similar problems).

### If "Windows protected your PC" appears

This tool's EXE is not code-signed (a decision for the first release). So at the first start, Microsoft Defender SmartScreen may show
"Windows protected your PC".

1. Click "More info" in the text of the window.
2. "App: GDSkinStudio.exe" and "Publisher: Unknown publisher" are shown; check the app name.
3. Click "Run anyway", which appears at the bottom.

Check the SHA-256 of the ZIP first (step 2 of the installation). On a PC with Windows 11 "Smart App Control" turned on,
the app may be blocked as an unsigned app, and no "Run anyway" button appears. In that case, this tool cannot be used.

## First-run setup

### Switching the screens to English

The app starts in Japanese (the default display language). To use it in English, click 設定 (Settings) on the left,
choose **English** in 表示言語（Language） (Display language), click 設定を保存 (Save settings), and restart the app.
After the restart, the app, including the setup screens, is shown in English. You can do this before the setup
([Advanced settings and additional features](#advanced-settings-and-additional-features)).

### Setup steps

At the first start, the "Setup" page opens. Go through the screens in order (you can redo the setup at any time from "Setup" on the left).

1. **Check environment**: Checks the OS, GPU and GPU memory, driver, Visual C++ Redistributable, free space,
   Grim Dawn location, whether Ashes of Malmouth is installed, and WebView2 (the data and temporary folder paths, and whether the game's bundled ArchiveTool.exe and TextureCompiler.exe are present, are shown in "Environment check" in Settings).
2. **Components and consent**: Check the names, sizes, license names and sources of what will be downloaded (the TRELLIS runtime, the TRELLIS models, DINOv2, and u2net for background removal).
   "Full license" shows the full text. Items that need attention are marked, such as GPL items (plyfile), items that include parts under a non-commercial license (kaolin), and items with a notice (CLIP).
   Text → 3D (TRELLIS-text-xlarge and CLIP) is optional.
   By default, the destination is inside the data folder (`%LOCALAPPDATA%\GrimDawnSkinMOD\trellis-runtime`). However, if that location cannot be used, for example because your user folder name contains full-width characters,
   it is `%SystemDrive%\GDSkinStudio\trellis-runtime` directly under the system drive (`C:\GDSkinStudio\trellis-runtime` on most PCs).
   You can also choose another drive with more free space. Use a path of 80 characters or fewer that contains no characters other than ASCII letters, digits and symbols (such as Japanese or full-width characters).
   The root of a drive (such as `D:\`), the Windows and Program Files folders, the Grim Dawn folder, and the data folder itself (and folders that contain it, such as your user folder) cannot be the destination
   (the reason is shown, and "Start download" cannot be clicked). Choose the default destination or a dedicated folder such as `D:\trellis-runtime`.
3. **Download**: When you agree, the download starts. Files are verified with hashes (SHA-256). If the connection drops, the download is retried automatically, and if you cancel, the next download continues where it left off
   (about 10 minutes to 1 hour, depending on your connection). If the GPU has at least 12 GB but less than 16 GB of memory, or the Visual C++ Redistributable is old or missing,
   a confirmation or warning is shown before the download starts.
4. **Reference meshes**: Extracts the game's body meshes used for the conversion from your Grim Dawn (the game folder is not changed).
5. **Launch options**: Shows how to set the Steam launch option `/basemods` ([below](#steam-launch-option-basemods)).

> **PCs that do not use 3D generation (such as PCs without a usable GPU)**: Without the TRELLIS runtime, you cannot go from "3. Download"
> to "4. Reference meshes" and "5. Launch options" (a limitation of this app). Close the setup with "Do it later", and prepare the reference
> meshes with "Prepare from the game" in Settings. See [below](#steam-launch-option-basemods) and "How to set /basemods" on the MOD page for
> setting the launch option `/basemods`. "GLB file" conversion and building and installing MODs still work (some GLB files
> need a recent Visual C++ Redistributable for the conversion; see [System requirements](#system-requirements)).

You can check the installed items and their licenses in "Setup (installed items and licenses)" in Settings.
You can re-verify, reinstall or remove the downloaded runtime in "TRELLIS runtime management (re-verify, reinstall, remove)" in Settings
([Troubleshooting](#troubleshooting), [Uninstalling](#uninstalling)).

> **Accepting the CLIP notice**: The CLIP notice also includes the model card's point that, because CLIP has not been trained or evaluated in languages other than English,
> its use should be limited to English. When the notice changes, you are asked to accept it again before using text → 3D, even if you accepted an earlier notice.
> The notice is shown in the display language (Japanese or English). Accepting the notice in either language is valid (the acceptance record keeps the language of the notice that was shown).

> **Accepting the licenses again when they change**: Because acceptance is recorded for the exact content shown, when an app update changes the license information,
> you are asked once to accept again the next time you download or repair
> (check the licenses in "2. Components and consent" on the Setup page and accept again; files already downloaded are verified and reused).
> Until you accept again, the setup download and "Reinstall Python and packages" in Settings stop and ask for acceptance, and "Re-verify models" does not re-download files that do not match.
> Generation with the runtime you have already downloaded keeps working.

> **The non-commercial part of kaolin**: kaolin 0.18.0, one of the downloaded packages, has the license name Apache License 2.0, but
> the included `kaolin/non_commercial` (the FlexiCubes implementation inside kaolin) is under the NVIDIA Source Code License and may be used only non-commercially (for research or evaluation purposes).
> TRELLIS uses only a small part of kaolin, but loading kaolin also loads this part. This part is shown on the setup and settings screens
> (the "Includes files under a non-commercial license" mark and a note) and in the license list. Use it only within the terms of this license.

## Usage

The app's "Help" page (in the left navigation; press F1 on any screen) explains each screen and its fields, how to install a MOD and undo it, and what to do when something goes wrong.

1. **Generate**: In "Generate" on the left, select an image of one character showing the whole body (PNG, JPEG, WebP or BMP; a PNG with a transparent background works best.
   "Check background removal" lets you check the image with the background removed in advance). You can also create a model from text (English; requires the optional download).
   Click "Start generation" to run the 3D generation and the conversion for male and female characters (about 1 to 2 minutes).
   - When you make a transparent PNG yourself, make the background fully transparent and the character opaque. Semi-transparent parts (light, hair, shadows, etc.) disappear or become dark.
     The background is not removed from an image that is almost entirely opaque, such as a screenshot with only transparent corners (a warning is shown). In the image field, turn on
     "Advanced settings for this image" → "Force background removal", or save the image again as a PNG with a transparent background or as a JPEG.
   - HEIC images, such as iPhone photos, cannot be used. Convert them to JPEG or PNG before selecting them.
   - If the subject is cut off at the edge of the image or is small, or there are several separate objects, "Notes about the subject" appears in the image field (generation is not stopped).
   - For text, "Template" under the text field inserts an example sentence or a prefix with conditions. The generation AI reads up to 77 tokens, including the two start and end markers
     (roughly 50 to 60 English words); an estimate of the token count and, when the text is too long, the part that is not used are shown.
     If the text contains words with non-English characters, or full-width letters, digits or symbols (including （ ） ～), a warning asks you to fix them. Without colors, the body tends to come out white or gray, so also write the colors of the hair, clothes, shoes and so on in English.
2. **Preview**: Rotate and inspect the generated model and the male and female results. If the front faces backwards, fix it with "Rotate 180° and reconvert".
   If the pose or a weapon bothers you, try another seed with "Regenerate with another seed". The "Candidates" tab of the preview lists the candidates compared by the automatic selection
   (arm angle, color, reason for exclusion, possible weapon); choose another candidate and remake the model with "Use this candidate". If you stopped during the conversion,
   "Use this candidate" in the row of the current body redoes only the conversion.
3. **Save**: Give the equipment a name (English and Japanese) and click "Save as equipment" to add it to the Library. For the English name, ASCII letters, digits and symbols are recommended
   (the game's fonts in English have only ASCII characters, so Japanese text and characters such as é will probably not be shown; a warning is shown when you save).
4. **MOD**: In "MOD" on the left, click "Build MOD" → "Install to Grim Dawn". Before installing, the app checks that the game is not running
   and checks for conflicts with files of other MODs, and it backs up the original files. "Uninstall" returns to the state before the first install.
   If you close the app during an install, uninstall or similar operation, the app does not cancel it partway; it waits for the operation to finish and then closes.
5. **Game**: Set the Steam launch option `/basemods` and start Grim Dawn.
   1. Buy "Illusion: <name>" from the Quartermaster (Ellis) in Devil's Crossing and right-click it to add it to your collection (close the shop first: right-clicking an item in your inventory while the shop is open sells it back).
   2. Equip something on the head, chest, shoulders, hands, legs and feet (illusions can only be applied to equipped slots).
   3. At the Illusionist, apply your equipment's illusion to the head and invisible (transparent) illusions to the other slots.
      If you wear a medal, applying an invisible illusion to the medal as well should hide the badge on the chest (not confirmed in the game).

Other features:

- GLB files made with other tools can also be converted from the "GLB file" input (no GPU needed). For a GLB that references texture images or `.bin` as external files,
  only files referenced by a relative path from the GLB's folder are loaded (for a GLB that references a file by an absolute path, on another drive, in a network location, etc., the conversion stops without reading that file).
- "Reconvert" in the Library rebuilds saved equipment with the current version's conversion without redoing the 3D generation (then run "Build MOD" and "Install to Grim Dawn" again).
  A check box in the confirmation window also lets you reconvert with the front rotated 180° (or back to the direction before the rotation) (the box is unchecked by default).
- "Deleted equipment…" in the Library restores deleted equipment with the same internal ID (then build and install the MOD;
  whether illusions already obtained in the game keep working as the same items has not been confirmed in the game).
- "Consumable text…" in the Library changes the name and description (English and Japanese) of the consumable bought from the Quartermaster (blank means the default text; ASCII letters, digits and symbols are recommended in the English fields; the game wraps descriptions only at half-width spaces, so put a half-width space between phrases in Japanese descriptions, as the default text does).
- "Export…" and "Import file…" in the Library let you exchange your equipment with other users (`.gdskin`; the input images and prompts are not included by default).

### Advanced settings and additional features

Turn on the following features only when you use them. **All are off by default**.

| Where | Feature |
|---|---|
| Generate → "Generation settings" → "Advanced settings" | **Sampling**: Standard (the model's default, 25 steps) / Fast (12 steps; for image input, the settings of the official TRELLIS demo; about half the time per candidate; the shape and detail change even with the same seed) |
| Same as above | **Low VRAM mode**: unloads models that are no longer needed from GPU memory while the mesh is being built (peak use drops by about 3.5 to 4.5 GB, and each candidate takes about 2 seconds longer; measured on a 24 GB GPU) |
| Same as above | **Block network access**: during generation, refuses outgoing connections from the generation process (Python) and writes the attempted destinations to the log (connections that native code opens by itself are not covered. Generation does not use the network anyway; this setting makes sure of it) |
| Same as above | **MOD textures**: the maximum size of the textures made by the conversion: 2048 (default) / 1024 / 512 (how equipment made with 1024 or 512 looks in the game has not been confirmed) |
| Same as above | **Keep candidates**: keeps the generation result of each candidate in the draft, so that "Use this candidate" and "Rebuild GLB" under the preview work without redoing the 3D generation (see the note below) |
| Same as above | **Background removal**: removes the background in the app before generation and lets you choose post-processing (smoothing the mask, removing background color left at the edges, alpha matting). The effect depends on the image, so compare the results with "Check background removal" (the effect has not been evaluated yet) |
| Same as above | **Appearance remake** (experimental): shows "Remake appearance from text…" in the Library. Remakes only the colors, materials and decorations from English text while keeping the shape (pose) of saved equipment, and saves the result as new equipment (requires the optional text → 3D components and acceptance of the CLIP notice. Tested with only 1 sample and 2 texts; the colors may not match the text, and small details of the shape may change) |
| Generate → "Advanced settings for this image" in the image field | **Force background removal**: even for an image that is almost entirely opaque (such as a screenshot with only transparent corners), fills the transparent areas with white and then removes the background (requires u2net) |
| 3D preview | **Front marker**: shows an arrow on the floor and the label "Front (+Z)" at the in-game front (+Z). Depending on the window width, the check box appears at the right end of the model selection row or to the right of "Wireframe" (only while a 3D model is shown) |
| Settings → "Display language" | **English**: English screens (the default is Japanese). Choose English and click "Save settings"; the change takes effect after you restart the app (the backend restarts with the app). On the Japanese screens, the setting is 表示言語（Language） and the button is 設定を保存. All text the app shows is then in English, including messages from the backend and from the generation and conversion processes, error "Details", the environment check, the setup items and their license information, the mods folder table and newly written log lines; the CLIP notice is also shown in English. What stays in Japanese: log lines written before the switch (they stay in the language they were written in) and text you typed (such as names). Error messages from Windows that appear in the error "Details" (such as "The system cannot find the file specified") are in the display language of Windows. The developer tools (the command-line tools gdconvert and modbuilder, and the packaging and publishing scripts) and the document under docs/ (the multiplayer check steps) is in Japanese only. The English wording follows the game's official English terms but has not been reviewed by native English speakers. Even when the app cannot connect to the backend, the display language and the Python location can be saved |

- **Space used by Keep candidates**: about 45 MB per body (about 180 MB for 4 candidates, and twice that if the candidate selection reaches its second round). It is not included in equipment or share files.
  The generation results are kept until the draft is saved or discarded, and are deleted then. However, they are kept when "Keep intermediate generation files (drafts) after saving" in Settings is on,
  and for drafts whose conversion failed (failed drafts can be deleted from "Data usage and cleanup" in Settings).
- The values of the advanced generation settings are saved as the defaults for next time when you generate, and are recorded in the draft and the equipment. "Regenerate" in the Library restores Sampling, Background removal, MOD textures and each image's Force background removal setting
  to the recorded values (Low VRAM mode and Block network access keep their current settings).
- When you export equipment with a remade appearance to an equipment package (`.gdskin`), the internal ID of the original equipment and the SHA-256 of the original 3D model are included in the record.

Settings also has the following items (they run only when you click them).

- **Data usage and cleanup**: Shows the size of each item in the data folder and what cleanup can delete. "Delete old builds…", "Delete old install backups…",
  "Permanently delete deleted equipment…", "Delete saved input images…" and "Delete failed, interrupted or saved drafts…" show what will be deleted and what will be kept (with reasons) in a confirmation window,
  and delete after you check the box. **Deleted items cannot be restored.** The following are never deleted: the installed build, the build of the state before the current install (the state that "Remove the received MOD and restore" returns to),
  the latest successful build, the backups used for uninstalling and for returning to the state before the current install, the 5 newest backups, drafts being processed or shown, and the input images used in the image field of the Generate page.
  To regenerate equipment whose input image was deleted, select the image again. Nothing is deleted automatically (drafts left in progress are only marked "interrupted" at startup, not deleted).
- **TRELLIS runtime management (re-verify, reinstall, remove)**: Only destinations created by the app's setup are covered.
  "Re-verify models…" re-reads every file with SHA-256 and downloads again from the source only the files that do not match.
  "Reinstall Python and packages…" downloads Python, PyTorch and the packages again from their sources, replaces them and runs a self-test (up to about 4.2 GB is downloaded).
  "Remove the text → 3D models…" and "Remove the whole TRELLIS runtime…" remove only what the app downloaded (the Library, built MODs and settings are not removed).

> **About the images you use**: For 3D generation, use only images that you have the right to use. Publishing or distributing models made from images of other people's works or characters
> may infringe the rights of the rights holders.

## Steam launch option /basemods

The MOD that this tool makes uses the `/basemods` method (it is loaded in the main campaign). Set `/basemods` in the Steam launch options.
This tool does not change the launch options automatically (Steam could overwrite the settings file while it is running).

1. In the Steam library, right-click Grim Dawn → "Properties"
2. Under "General" → "Launch Options", enter `/basemods` (if there are other options, add it at the end, separated by a space)
3. Start Grim Dawn from Steam

- The app's launch option check (in "Environment check" in Settings, in "5. Launch options" of the setup and on the MOD page) reads the settings of the user currently logged in to Steam (if that cannot be determined, the user who logged in last)
  (it only reads them). Right after you set the option while Steam is running, the app may report that `/basemods` is not set until you exit Steam.
  Exit Steam, then click "Check again" (on the MOD page, "Refresh").
- Only one `mods\database.arz` is read, so this MOD cannot be used together with other main-campaign MODs.
- If you will not use other `/basemods` MODs after uninstalling, remove `/basemods` from the launch options.
- **Back up your saves**: Before you install a MOD for the first time and before you remove the MOD, it is recommended to copy the save folder (`Documents\My Games\Grim Dawn\save`;
  if you use Steam Cloud, also `Steam\userdata\<number>\219990\remote\save`) as a whole to another location.
  What happens to this tool's consumables and collected illusions when you start the game without `/basemods`, or load the character in the Crucible or a Custom Game,
  has not been tested.

## Multiplayer (unverified)

**Multiplayer has not been tested.** The following are expectations.

- In multiplayer, everyone who joins is expected to need the same MOD (the same `database.arz`).
  Players whose MODs contain different sets of equipment may not be able to join each other, or may see things differently.
- For this, the app can make a share file (`.gdskinmod`) that hands over your MOD as is. Export it with "MOD" → "3. Sharing for multiplayer";
  the person who receives it installs it with "Import and install a share file…", byte for byte, without rebuilding. **The receiver's MOD is replaced**
  (it can be restored with "Remove the received MOD and restore").
- If the **verification code** shown on the screen (the code for the whole MOD: 12 characters made from the names and SHA-256 hashes of all files (`database.arz`, `Items.arc` with the appearance, and `Text_XX.arc` with the names),
  for example `1A2B-3C4D-5E6F`) is the same for everyone, everyone has the same MOD. The code for `database.arz` alone (the database code) is
  shown separately in the latest build field and on the confirmation screens.
- If the verification codes differ, "Verification details" on the "MOD" page shows the code for each file and which files differ (the database, or only the appearance and names).
  You can also enter the other person's verification code and click "Compare". The confirmation screen for a received MOD shows a comparison with the installed MOD and with your latest build.
- If the contents of a received MOD differ from what this tool creates (because of a different game version or changes made by the creator), the confirmation screen shows a warning for each item
  (these differences alone do not stop the import). Check the contents before installing.
- Do not use files received from people you do not know (hashes are not signatures).

The steps for checking are in [docs/multiplayer_check.md](docs/multiplayer_check.md) (Japanese). We would appreciate hearing your results.

## Troubleshooting

More detailed steps are in the app's "Help" page under "Troubleshooting".

| Symptom | What to do |
|---|---|
| "Windows protected your PC" appears when you start the app | This SmartScreen window appears because the app is not signed. You can start the app with the steps in [Installation](#if-windows-protected-your-pc-appears) ("More info" → "Run anyway"). Check the SHA-256 of the ZIP first. Antivirus software may block `GDSkinStudio.exe` or `python\python.exe` |
| "The app is already running" appears | Only one instance of the app can run with the same data folder. Use the window that is open. If the previous app is still closing, wait a moment and start it again (if a message says that another backend is running with the same data folder, wait a moment and click "Restart backend") |
| The app does not start / "The backend … has stopped" | Check that you extracted the ZIP before starting the app, that you did not delete files in the extracted folder (such as `python\`), and that antivirus software is not blocking `python\python.exe`. Logs are in `%LOCALAPPDATA%\GrimDawnSkinMOD\logs\` (if the backend stopped after starting, the end of the backend's output at that time is kept in `app-<date>.log` and in the ERROR lines on the Log page). If "Could not start Python (python.exe) for running the backend" appears, check "Backend Python" in Settings |
| "Cannot use the data folder" appears at startup and the app exits | The data folder (the location given with `--data-dir` or the `GDSKIN_HOME` environment variable, or the default location if none is given) or the `logs\` folder inside it cannot be created, or the path is malformed. Check the path you specified, the drive and the access permissions, then start the app again |
| The setup download fails | Click "Retry (continue)" (files already downloaded are verified and reused; if a connection to huggingface.co or another site was cut partway, retrying also continues the download from where it stopped). The download fails if connections to github.com (including the redirect targets codeload.github.com and release-assets.githubusercontent.com), huggingface.co (including the delivery servers it redirects to, such as us.aws.cdn.hf.co), releases.astral.sh, download.pytorch.org, pypi.org, files.pythonhosted.org, dl.fbaipublicfiles.com or nvidia-kaolin.s3.us-east-2.amazonaws.com are blocked, for example on a company or school network. "The source did not return the file (HTTP <number>)" means that the file at the source may have been moved or removed, or that a proxy or filter is blocking it (if it keeps happening, check for a new version of the app). "Could not write to a file being downloaded" means that another app or antivirus software may have the file open; wait a moment and retry. For "Not enough free space", choose a destination on a drive with more free space; for "The destination path is too long", use a short path of 80 characters or fewer |
| "The Visual C++ Redistributable is required" (setup self-test) | Install the latest Visual C++ Redistributable (x64) from [Microsoft's download link](https://aka.ms/vc14/vc_redist.x64.exe) (administrator rights are required), then click "Retry (continue)". Files already downloaded are reused |
| "No GPU found" (or "No supported GPU found"), "The GPU driver is too old", "Not enough GPU memory" | Update the NVIDIA driver to R570 or later. GPUs with less than 12 GB of GPU memory or with compute capability below 6.0 cannot be used for 3D generation. Close other apps (such as games) before generating. If the GPU has less than 16 GB of memory, or if generation fails for lack of memory, try "Low VRAM mode" in the advanced generation settings |
| "Could not load the models" / the setup self-test fails | Check the "Details" and the log. If the model files may be damaged, use "Re-verify models…" in "TRELLIS runtime management" in Settings (it re-reads every file and downloads again only the files that do not match); if Python or the packages may be damaged, use "Reinstall Python and packages…" (both cover only destinations created by the app's setup) |
| The re-verify result says "Download them again from the Setup page" | Some files did not match, but they were not downloaded again because there is no record of acceptance of the licenses and notices. Run "Setup" on the left; it downloads again only the files that do not match |
| An operation in "TRELLIS runtime management" in Settings (such as "Reinstall Python and packages…") fails and the hint mentions "Retry (continue)" | That button is on the Setup page, not in Settings (a limitation of this app). Fix the cause, then click the same button in Settings again |
| "A file needed for generation is missing" | Files are not downloaded during generation, so generation was stopped. Run "Setup" on the left (it downloads the missing files and verifies and reuses the files already downloaded). If files may be damaged, use "Re-verify models…" |
| "Reference meshes are missing" | Prepare them with "Prepare reference meshes" in the error window or in "4. Reference meshes" of the setup. If Grim Dawn is not found, specify its install folder in Settings |
| "The reference meshes are corrupted" / "Cannot open the reference meshes" | If they are corrupted, extract them again with "Prepare reference meshes" in the error window or "Prepare from the game" in Settings. If they cannot be opened (another app is using them, or you do not have permission to read them), close or pause antivirus, backup or sync software (OneDrive, etc.), or check that you have permission to read `refs\` in the data folder, then try again ("Prepare from the game" does not fix this; it skips files it cannot open) |
| Part of the character disappears / the background remains in an image with a background | Check with "Check background removal"; if it does not work well, use a PNG with a transparent background. You can also compare the "Background removal" advanced setting (background removal in the app with post-processing) using "Check background removal" |
| The background becomes part of the 3D model (a board or a stand) / "…almost all of it was opaque, so it was generated without removing the background" | The background is not removed from an image that is almost entirely opaque, such as a screenshot with only transparent corners. In the image field, turn on "Advanced settings for this image" → "Force background removal" and generate again, use a PNG with a transparent background, or save the image again as a JPEG |
| The conversion shows "Some vertices are extremely far from the body" / the quality note "Some vertices were far away from the body…" appears | The GLB has broken or extra parts away from the body (such as small fragments or stray points). If they are extremely far away (more than 100 times the body height), the GLB cannot be converted; remove those parts in the tool that made the GLB, then export it again. If they are closer, the conversion continues and leaves them out only when fitting the size and position (the parts remain in the equipment). Check the preview, and remove them in the same way if they are not needed |
| The feet or shins are distorted or torn in the game | For equipment converted with an earlier version of the app, click "Reconvert" in the Library, then run "Rebuild the MOD" → "Install to Grim Dawn". If they are still distorted, try another seed with "Regenerate", or use an image of a standing pose with the arms and legs slightly apart |
| "The MOD build stopped partway" | The mods folder was not changed. The "Details" in the error window show the cause, such as the output of the game's bundled tools |
| "The game's bundled tool is missing" / the environment check shows ArchiveTool.exe or TextureCompiler.exe as "not found" | You can restore them in the Steam library: Grim Dawn's Properties → "Installed Files" → "Verify integrity of game files" |
| Installing or uninstalling failed | If writing fails, the mods folder is returned to its state before the operation. If a message says that some files could not be restored, close Grim Dawn and any programs that have the files open, then do the same operation again (the files from before the operation are backed up in `install\backups\<ID>` in the data folder). If the mods folder contains read-only files, the operation stops at the check before any change and tells you (clear "Read-only" in the file's properties in File Explorer). If "Could not save the install record" appears, the mods folder has been changed, but the install record (`install\` in the data folder) is out of date. As when the connection to the backend is lost partway, check the current state in the "mods folder status" table on the "MOD" page. If the completion message says there are warnings ("Done (with warnings)"), read the warnings ("Could not update the install status in the equipment library" means that the changes to the mods folder are complete, but marks such as "Installed" in the Library may be out of date; the log has the reason) |
| The 3D preview says "The 3D preview cannot be shown on this PC (WebGL is not available…)" | Updating the graphics driver may help. You can also check the conversion result on the "Conversion check image" tab of the preview |
| After a game update (patch) / a warning banner appears on the "MOD" page | The MOD contains a copy of the Quartermaster's stock (official records), so when a game update changes the stock, the MOD overwrites it with the old contents. If the banner lists changed tiers, rebuild with "Rebuild the MOD" ("Build MOD"), then install again with "Install the latest build" ("Install to Grim Dawn"). If the latest build already matches the current game, installing it again is enough. The "Game version" row shows the Steam build numbers (when the MOD was built and now) and one of the following: the same version; updated, but the stock has not changed, so the MOD can be used as is; a MOD that matches the current game is needed; or not compared because there is no record |
| A warning banner appears after the game was updated while you use a received MOD | Ask the person who made the MOD to rebuild it with the current game and share it again, then receive and install it again. If you replace it with your own MOD, your verification code will no longer match theirs |
| An old AI Skin MOD remains after uninstalling | Uninstalling returns to "the state before the first install". If the mods folder contained an AI Skin MOD with no record in this app (for example, a prototype) at that time, the folder returns to that MOD, and the game loads it (the MOD page and the confirmation window show this). If you do not need it, exit the game, open the folder with "Open mods folder", and move those files somewhere else |
| Your verification code does not match the other person's | Check which files differ in "Verification details" on the "MOD" page. If `database.arz` is the same and only the appearance (`Items.arc`) and names (`Text_XX.arc`) differ, you are expected to be able to connect, but each of you sees different appearances or names for the equipment |
| The illusion does not appear in the game / it is not in the Quartermaster's stock | Check that the Steam launch options include `/basemods` and that the MOD is installed on the "MOD" page. It cannot be used together with other `/basemods` MODs (also check for "Other file (loaded)" in the table on the MOD page). The Quartermaster's stock appears at Survivors reputation of Friendly or higher. Illusions can only be applied to equipped slots, so also check that you have something equipped on the head, chest, shoulders, hands, legs and feet (step 5 of [Usage](#usage)) |
| Item names and descriptions are not shown correctly when the game is set to a language other than English | The MOD includes name files for 13 languages (`Text_XX.arc`) (Japanese names for Japanese, English names for the other languages). The Japanese names and descriptions were confirmed in the game set to Japanese (2026-11-18). Whether the game reads them when it is set to a language other than English and Japanese has not been confirmed in the game yet. We would appreciate hearing your results |
| Some text is still in Japanese on the English screens | A change of display language takes effect after you restart the app (until then, the previous language is shown). Log lines written before the switch stay in the language they were written in. Text you typed (such as names) is shown as you typed it. Error messages from Windows in the error "Details" are in the display language of Windows. The developer tools (the command-line tools gdconvert and modbuilder, and the packaging and publishing scripts) and the document under docs/ (the multiplayer check steps) is in Japanese only. If you find Japanese text anywhere else, please let us know; see [CONTRIBUTING.md](CONTRIBUTING.md) (Japanese) |
| Equipment made while the app was started from inside another app does not appear when you start the app normally | When the app is started from inside some apps (MSIX-packaged apps), the data folder may be split off into a different location (a virtualized folder). Start the app normally, for example from File Explorer, and import the equipment with "Import…" in the Library |

The logs (`app-<date>.log` and `backend-<date>.log` in `%LOCALAPPDATA%\GrimDawnSkinMOD\logs\`) do not contain the contents of the images or text you enter.
When you report a problem, check that the logs do not contain your folder names or similar information before attaching them.

## Uninstalling

1. **Remove the MOD installed in the game**: Click "Uninstall" on the app's "MOD" page (this returns to the state before the first install).
   Also remove `/basemods` from the Steam launch options if needed.
   If there was an old AI Skin MOD with no record in this app (for example, a prototype) before the first install, the folder returns to that MOD (the screen shows this;
   see [Troubleshooting](#troubleshooting)).
2. **Remove the 3D generation runtime (optional)**: Before you remove the app, you can remove the runtime and models the app downloaded (about 13 GB, or about 19 GB with the text → 3D items) with "Remove the whole TRELLIS runtime…"
   in "TRELLIS runtime management" in Settings (only destinations created by the app's setup are covered; other files you placed there are kept).
3. **Remove the app**: Delete the extracted folder as a whole (nothing is registered in the registry or the Start menu).
4. **Remove the data (optional)**: **Do the uninstall in step 1 first.** If the install replaced files of another MOD,
   the original files exist only in `install\backups` in the data folder, and deleting the data folder makes it impossible to restore them.
   Then delete `%LOCALAPPDATA%\GrimDawnSkinMOD`. This removes your equipment, MOD builds, install records and backups, settings, logs, and the
   3D generation runtime and models (if they are in the default location). If the runtime destination is somewhere else (a location you chose during the setup, or
   `%SystemDrive%\GDSkinStudio\trellis-runtime`, which becomes the default when your user folder name contains full-width characters, etc.), do step 2 or delete that folder as well.

To replace the app with a new version, do only step 3 and extract the new ZIP. The data folder is kept, so your equipment and downloaded models can be used as they are.

### If the app cannot remove the MOD (no install record)

If you deleted the data folder, or installed with a data folder in another location (such as the virtualized folder case in [Troubleshooting](#troubleshooting)),
"Uninstall" in the app cannot be clicked (if you can still click it, it stops with "No MOD has been installed by this app"), and the files in the mods folder remain.

1. If you do not use other `/basemods` MODs, remove `/basemods` from the Steam launch options (this alone stops the game from loading the files directly in the mods folder).
2. To delete the files as well, exit Grim Dawn and check how the "MOD" page of the app classifies the files in the mods folder.
   Delete only the `database.arz`, `Items.arc` and `Text_XX.arc` files directly in the mods folder that are shown as "Installed by this app" or "AI Skin MOD with no record"
   (`Text_CS.arc` to `Text_ZH.arc` for 13 languages; MODs made with earlier versions have only `Text_EN.arc` and possibly `Text_JA.arc`).
   Do not delete files shown as "Another MOD or unknown file". Do not delete folders such as `mods\survivalmode` (the Crucible DLC) either.

It is recommended to back up your game saves before you remove the MOD ([Steam launch option /basemods](#steam-launch-option-basemods)).

## Where data is stored

| Location | Contents |
|---|---|
| Extracted folder | The app itself (`GDSkinStudio.exe`, `python\`, `web\`, `app\backend\`, `src\`, `scripts\trellis\`, `licenses\`, `README.txt` and `README.en.txt` (English), `THIRD_PARTY_NOTICES.txt` and `THIRD_PARTY_NOTICES.en.txt` (English), `LICENSE.txt`, `MANIFEST.txt`). Replace it as a whole when updating |
| `%LOCALAPPDATA%\GrimDawnSkinMOD` | Data folder (kept when the app is updated; can be changed with `--data-dir`): `library\` (your equipment), `builds\` (MOD builds), `install\` (install records and backups of files from before installing, including the original files of other MODs that were replaced), `refs\` (reference meshes extracted from the game), `trellis-runtime\` (3D generation runtime and models; the default destination), `work\` (generation drafts), `shared\` (received MODs), `logs\` (deleted automatically after 14 days), `app\` (settings). For the size of each item and cleanup, see "Data usage and cleanup" in Settings |
| `%SystemDrive%\GDSkinStudio\trellis-runtime` | The default destination when the data folder cannot be used, for example because your user folder name contains full-width characters |
| `<Grim Dawn>\mods\` | The installed MOD (`database.arz`, `Items.arc`, and `Text_XX.arc` for 13 languages). Uninstalling returns it to the state before the first install |

## For developers

This repository contains only what is needed to run the app and to make the distribution ZIP.

| Folder | Contents |
|---|---|
| `app/frontend/` | The app's screens (C# / .NET 10 WPF; the 3D preview uses WebView2 and the bundled three.js) |
| `app/backend/` | The app's backend (Python; runs generation, conversion and MOD building as jobs) |
| `src/` | Components: `gdmesh` (reading and writing `.msh`), `gdconvert` (GLB → male and female `.msh` and `.tex`), `modbuilder` (building, validating and installing the MOD) |
| `scripts/trellis/` | The 3D generation (TRELLIS) scripts and the definitions (versions and hashes) of what the first-run setup downloads |
| `scripts/package/`, `scripts/publish/` | Making and checking the distribution ZIP, and the pre-publication check (that no personal information, game files and the like are included) |
| `scripts/gdconvert.py`, `scripts/modbuilder.py` | Command-line entry points of the components (see `--help`) |
| `licenses/` | Full texts of third-party licenses (what the first-run setup downloads, and the .NET components bundled in the distribution ZIP) |
| `docs/multiplayer_check.md` | Steps for checking the appearance in multiplayer (Japanese) |

The tests, the UI automation tool, the prototypes and the research and design records used during development are not included. The `docs/` documents and
numbers such as D-001, B-001 or 点検 C14 mentioned in code comments refer to development records that are not part of this repository.
The files used by `-Mode dev` (development comparison components) of `scripts/trellis/setup_trellis_env.ps1` are not included either (the files used by `-Mode runtime` are).

### Requirements

| Item | Used for |
|---|---|
| Windows 10 / 11 (64-bit) | Everything |
| .NET 10 SDK | Building the screens (WPF) |
| Python 3.11 (the `py` launcher) with `numpy`, `scipy`, `Pillow`, `lz4`, `fast-simplification` and `xatlas` | Backend, conversion and MOD building (the versions and hashes bundled in the distribution ZIP are in `scripts/package/backend-requirements.txt`) |
| git | Making the distribution ZIP (it checks that the files it copies are tracked by git) |
| Grim Dawn (Steam version) and Ashes of Malmouth | Conversion and MOD building (the game's files are only read) |

```bash
py -3.11 -m pip install numpy scipy Pillow lz4 fast-simplification xatlas
```

### Running from source

```bash
dotnet build app/frontend/GDSkinStudio.sln -c Debug
```

```bash
app/frontend/GDSkinStudio/bin/Debug/net10.0-windows/GDSkinStudio.exe
```

- The Python that runs the backend is searched for in this order: the one set in "Backend Python" in Settings → `%LOCALAPPDATA%\Programs\Python\Python311\python.exe` → `py -3.11`.
- The default data folder is `%LOCALAPPDATA%\GrimDawnSkinMOD`, the same as the app from the distribution ZIP. When you try things out, use another location with `--data-dir <folder>`.
- The 3D generation runtime is downloaded with the app's "Setup".

### Making the distribution ZIP

```bash
py -3.11 scripts/package/build_package.py
```

- This creates `build/package/GDSkinStudio-<version>-win-x64.zip` and a folder with the same contents as the extracted ZIP.
- It downloads the .NET packages (NuGet), the Python embeddable package (python.org; its size, SHA-256 and MD5 are verified) and the Python packages (PyPI; their hashes are verified).
- It stops if there is a file not tracked by git in the copied area. At the end, it runs the check of `scripts/package/check_package.py`.

How to contribute changes is in [CONTRIBUTING.md](CONTRIBUTING.md) (Japanese), and the change history is in [CHANGELOG.md](CHANGELOG.md) (Japanese).

## License and third-party notices

This tool (GD Skin Studio and its Python components: this project's code in `app/`, `src/` and `scripts/`) is released under the MIT License ([LICENSE](LICENSE)).

Copyright (c) 2026 歌うＬＩＮＥ彼氏

The following are not covered by the MIT License; each is under its own license.

| Item | License | Full text |
|---|---|---|
| three.js r170 (included in this repository and the distribution ZIP; 3D preview) | MIT | [app/frontend/GDSkinStudio/web/vendor/three/LICENSE](app/frontend/GDSkinStudio/web/vendor/three/LICENSE) |
| Bundled in the distribution ZIP: .NET 10 runtime, WebView2 SDK, Python 3.11.9, numpy, scipy, Pillow, lz4, fast-simplification, xatlas | MIT, BSD-3-Clause, PSF-2.0, MIT-CMU, etc. | `THIRD_PARTY_NOTICES.en.txt` (English; Japanese: `THIRD_PARTY_NOTICES.txt`) and `licenses\bundled\` in the ZIP |
| Downloaded during the first-run setup (not bundled): TRELLIS and the TRELLIS models, DINOv2, u2net, about 140 packages such as PyTorch, and optionally CLIP | MIT, Apache-2.0, BSD-3-Clause, etc., including **plyfile under GPL-3.0-or-later**, **kaolin, which includes a part under the NVIDIA Source Code License (non-commercial, research or evaluation purposes only)**, packages under LGPL and MPL, and CLIP with a notice | [licenses/setup/](licenses/setup/), the app's setup screen |

The list of what is downloaded and the full license texts are in [licenses/setup/](licenses/setup/) and in `THIRD_PARTY_NOTICES.en.txt` of the distribution ZIP. This repository and the distribution ZIP contain no Grim Dawn files.
The reference meshes and the MOD template (official records) are read from the game installed by the user.

## Disclaimer

- This is an unofficial fan-made tool. It is not affiliated with Crate Entertainment, Microsoft, Meta, OpenAI, NVIDIA, Valve or any other developer or provider of the
  software, models and services this tool uses, and it is not approved or endorsed by them.
- Grim Dawn is a trademark of Crate Entertainment. Other names are trademarks or registered trademarks of their respective owners.
- Install MODs at your own risk. The original files are backed up before installing, but backing up your saves separately is recommended.
- The quality, shape and content of the generated 3D models are not guaranteed. You are responsible for the images and text you enter and for publishing or distributing the models and MODs you make.
- This software is provided "as is", without warranty of any kind, express or implied ([LICENSE](LICENSE)).
