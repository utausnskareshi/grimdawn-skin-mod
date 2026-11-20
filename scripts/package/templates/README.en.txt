日本語の説明は README.txt を参照してください。

GD Skin Studio {version} (Windows 64-bit)
=========================================

A tool that makes Grim Dawn MODs that "turn AI-made 3D models into illusion equipment".
It generates a 3D model from an image (or text), converts it for Grim Dawn's male and female characters,
builds a MOD that you can use as a head illusion, and installs it into the game folder.

The images and text you enter are never sent outside this PC (3D generation also runs on this PC's GPU).
The tool connects to the internet only when the setup downloads the TRELLIS runtime and models (the first download,
and re-verifying or reinstalling with "TRELLIS runtime management" in Settings). During 3D generation, missing files
are not downloaded: generation stops with an error (default behavior). Turning on "Block network access" in the
advanced generation settings refuses outgoing connections from the generation process (Python) (connections that
native code opens by itself are not covered).

This is an unofficial fan-made tool. It is not affiliated with Crate Entertainment (the developer of Grim Dawn),
Microsoft (the developer of TRELLIS), or any other developer of the software and models this tool uses.

Official site (latest version, source code, bug reports): https://github.com/utausnskareshi/grimdawn-skin-mod
Get the distribution ZIP from Releases on the official site (https://github.com/utausnskareshi/grimdawn-skin-mod/releases).

{copyright}
This tool is provided under the MIT License (LICENSE.txt).

The app starts in Japanese (the default display language). To use it in English, click 設定 (Settings) on the left,
choose "English" in 表示言語（Language） (Display language), click 設定を保存 (Save settings), and restart the app
(see "Settings → Display language" in chapter 3).


Contents
--------
  1. System requirements
  2. Installation
  3. Usage
  4. Steam launch option /basemods
  5. Troubleshooting
  6. Uninstalling (removal)
  7. Folder layout and where data is stored
  8. Licenses


1. System requirements
----------------------

  Required
    - Windows 10 / 11 (64-bit)
    - Grim Dawn (Steam version) and the expansion "Ashes of Malmouth"
      (the MOD template uses Ashes of Malmouth data)
    - Free disk space: about {app_size} for the app, about 13 GB for the TRELLIS runtime and models
      (more is needed temporarily while downloading; for a new download, the setup asks for about 18 GB of free space
      and shows this on the setup screen; adding text → 3D takes about 6 GB more)
    - An internet connection for the first-run setup (about 8.6 GB is downloaded)

  Needed for 3D generation (from images and text)
    - An NVIDIA GPU (12 GB or more of GPU memory, 16 GB or more recommended, compute capability 6.0 or higher)
    - NVIDIA driver R570 or later (one that supports CUDA 12.8)
    - A recent version of the Microsoft Visual C++ Redistributable (x64)
      (an old version is often installed together with Grim Dawn, and with the old version, the self-test after the
      download fails. If the environment check reports it as old or missing, install the latest version from
      https://aka.ms/vc14/vc_redist.x64.exe . Administrator rights are required)
    Note: On a GPU with at least 12 GB but less than 16 GB of GPU memory, a warning is shown before the setup
      download starts, and you can choose whether to continue. The TRELLIS README requires 16 GB or more, and
      generation on GPUs of this size has not been tested. If generation fails for lack of memory, try
      "Low VRAM mode" in the advanced generation settings (on a 24 GB GPU, peak use was measured to drop from
      about 14 GB to about 10 GB; whether it works on 12 to 16 GB GPUs has not been confirmed).
    Note: The tool has been tested only with a GeForce RTX 5090 Laptop GPU (Windows 11).
      It has not been tested on other GPUs (RTX 40 series or older, etc.).
    Note: Even without a GPU, you can convert GLB files made with other tools and build and install MODs.
      However, converting a GLB with more than 25,000 vertices or with several materials or textures uses a
      component (for rebuilding the UVs) that relies on the Windows Visual C++ runtime, so the recent Visual C++
      Redistributable above is required (what happens on a PC with only an old version has not been tested).

  Not needed
    - You do not need the .NET runtime, Python, Visual Studio, the CUDA Toolkit or git
      (everything needed is either bundled or downloaded during the first-run setup).
      Administrator rights are not needed either (except to install or reinstall the Visual C++ Redistributable).
    - The 3D preview uses the Microsoft Edge WebView2 Runtime. It is included with Windows 11.
      If it is missing, install it with the "Evergreen Bootstrapper" on
      https://developer.microsoft.com/microsoft-edge/webview2/ .


2. Installation
---------------

  (0) (Recommended) Check that the downloaded ZIP is not damaged or tampered with.
      Compare the SHA-256 of the ZIP shown on the download page (GitHub Releases) with the SHA-256 of your ZIP.
      Open PowerShell in the folder that contains the ZIP and run the following (adjust the file name to your ZIP):
          Get-FileHash .\GDSkinStudio-{version}-win-x64.zip -Algorithm SHA256
      In Command Prompt:
          certutil -hashfile GDSkinStudio-{version}-win-x64.zip SHA256
      If the 64 characters shown match the value on the download page (uppercase and lowercase letters count as
      the same), the file is the same as the one distributed.
      If they do not match, do not use it; download it again.
      (The SHA-256 of each file after extraction is in MANIFEST.txt)

  (1) Extract the ZIP file to a folder of your choice.
      Example: C:\Tools\GDSkinStudio
      Note: Avoid "C:\Program Files", where writing is restricted.
      Note: A short folder path without full-width characters (such as Japanese) or non-English characters (such as é)
        is recommended. The same applies to the data folder (by default %LOCALAPPDATA%\GrimDawnSkinMOD). If your
        Windows user name contains full-width characters, the default data folder path contains them too. A workaround
        for the game's bundled tools not handling such paths is included, but it has not been tested on a real PC with
        a full-width user name. You can change the location of the data folder with
        "GDSkinStudio.exe --data-dir <folder>" (a short path with only ASCII letters, digits and symbols is
        recommended; "Data and temporary folder paths" in "Environment check" in Settings tells you about paths that
        are too long and similar problems).
      Note: Do not run the app from inside the ZIP; always extract it.

  (2) Start GDSkinStudio.exe in the extracted folder (only one instance of the app can run with the same data folder).
      This tool's EXE is not code-signed (a decision for the first release). So at the first start, Microsoft
      Defender SmartScreen may show a blue window saying "Windows protected your PC" and "Microsoft Defender
      SmartScreen prevented an unrecognized app from starting" (only the "Don't run" button seems to be available).
        1. Click "More info" in the text of the window.
        2. "App: GDSkinStudio.exe" and "Publisher: Unknown publisher" are shown; check the app name.
        3. Click "Run anyway", which appears at the bottom.
      In most cases, the window is not shown again for the same file. Check the SHA-256 in (0) before running the app.
      - Before you extract the ZIP, you can right-click the ZIP → "Properties", check "Unblock" next to
        "Security: This file came from another computer and might be blocked to help protect this computer" at the
        bottom of the "General" tab, and click "OK"; then this window is not shown for the extracted files (do this
        only for a ZIP whose source you have checked).
      - On a PC with Windows 11 "Smart App Control" turned on, the app may be blocked as an unsigned app, and no
        "Run anyway" button appears. In that case, this tool cannot be used.

  (3) At the first start, the "Setup" page opens. Go through the screens in order.
      (The app starts in Japanese. To switch to English first, see the note at the top of this file.)
      1. Check environment: Checks the OS, GPU and GPU memory, driver, Visual C++ Redistributable, free space,
         Grim Dawn location, whether Ashes of Malmouth is installed, and WebView2 (the data and temporary folder
         paths, and whether the game's bundled ArchiveTool.exe and TextureCompiler.exe are present, are shown in
         "Environment check" in Settings).
      2. Components and consent: Check the names, sizes, license names and sources of what will be downloaded
         (the TRELLIS runtime, the TRELLIS models, DINOv2, and u2net for background removal). "Full license" shows
         the full text. GPL items (plyfile), items that include parts under a non-commercial license (kaolin) and
         items with a notice (CLIP) are marked.
         Text → 3D (TRELLIS-text-xlarge and CLIP) is optional. CLIP requires accepting the notice in its model card.
         By default, the destination is inside the data folder (%LOCALAPPDATA%\GrimDawnSkinMOD\trellis-runtime).
         However, if that location cannot be used, for example because your user folder name contains full-width
         characters, it is %SystemDrive%\GDSkinStudio\trellis-runtime directly under the system drive
         (C:\GDSkinStudio\trellis-runtime on most PCs).
         You can also choose another drive with more free space. Use a path of 80 characters or fewer that contains
         no characters other than ASCII letters, digits and symbols (such as Japanese or full-width characters).
      3. Download: When you agree, the download starts. Progress and sizes are shown, and files are verified with
         hashes (SHA-256). If the connection drops, the download is retried automatically, and if you cancel, the next
         download continues where it left off.
         It takes about 10 minutes to 1 hour, depending on your connection.
         If the GPU has at least 12 GB but less than 16 GB of memory, or the Visual C++ Redistributable is old or
         missing, a confirmation or warning is shown before the download starts.
      4. Reference meshes: Extracts the game's body meshes used for the conversion from your Grim Dawn
         (the game folder is not changed).
      5. Launch options: Shows how to set the Steam launch option /basemods (chapter 4).
      On a PC that does not use 3D generation (such as a PC without a usable GPU), you cannot go from "3. Download"
      to "4. Reference meshes" and "5. Launch options" without the TRELLIS runtime (a limitation of this app). Close
      the setup with "Do it later" and prepare the reference meshes with "Prepare from the game" in Settings. See
      chapter 4 and "How to set /basemods" on the MOD page for setting the launch option. "GLB file" conversion and
      building and installing MODs still work. Some GLB files need a recent Visual C++ Redistributable for the
      conversion (chapter 1).
      You can redo the setup at any time from "Setup" on the left or "Redo setup" in Settings.
      You can check the installed items and their licenses in "Setup (installed items and licenses)" in Settings.
      You can re-verify, reinstall or remove the downloaded runtime in
      "TRELLIS runtime management (re-verify, reinstall, remove)" in Settings (chapters 3, 5 and 6).

      Note: Accepting the CLIP notice: The CLIP notice also includes the model card's point that, because CLIP has
        not been trained or evaluated in languages other than English, its use should be limited to English.
        When the notice changes, you are asked to accept it again before using text → 3D, even if you accepted an
        earlier notice.
        The notice is shown in the display language (Japanese or English). Accepting the notice in either language
        is valid (the acceptance record keeps the language of the notice that was shown).

      Note: Accepting the licenses again when they change: Because acceptance is recorded for the exact content
        shown, when an app update changes the license information, you are asked once to accept again the next
        time you download or repair (check the licenses in "2. Components and consent" on the Setup page and accept
        again; files already downloaded are verified and reused). Until you accept again, the setup download and
        "Reinstall Python and packages" in Settings stop and ask for acceptance, and "Re-verify models" does not
        re-download files that do not match. Generation with the runtime you have already downloaded keeps working.

      Note: The non-commercial part of kaolin: kaolin 0.18.0, one of the downloaded packages, has the license name
        Apache License 2.0, but the included kaolin/non_commercial (the FlexiCubes implementation inside kaolin) is
        under the NVIDIA Source Code License and may be used only non-commercially (for research or evaluation
        purposes). TRELLIS uses only a small part of kaolin, but loading kaolin also loads this part. This part is
        shown on the setup and settings screens (the "Includes files under a non-commercial license" mark and a
        note) and in the license list. Use it only within the terms of this license.


3. Usage
--------

  The app's "Help" page (in the left navigation; press F1 on any screen) explains each screen and its fields, how to
  install a MOD and undo it, and what to do when something goes wrong.

  (1) Generate: In "Generate" on the left, select an image of one character showing the whole body (PNG, JPEG, WebP
      or BMP; a PNG with a transparent background works best. The background of an image with a background is
      removed automatically. "Check background removal" lets you check this in advance).
      You can also add back and side views (up to 4 images). You can also create a model from text (English;
      requires the optional download).
      Click "Start generation" to run the 3D generation and the conversion for male and female characters
      (about 1 to 2 minutes).
      - When you make a transparent PNG yourself, make the background fully transparent and the character opaque.
        Semi-transparent parts (light, hair, shadows, etc.) disappear or become dark. The background is not removed
        from an image that is almost entirely opaque, such as a screenshot with only transparent corners (a warning
        is shown). In the image field, turn on "Advanced settings for this image" → "Force background removal",
        or save the image again as a PNG with a transparent background or as a JPEG.
      - HEIC images, such as iPhone photos, cannot be used. Convert them to JPEG or PNG before selecting them.
      - If the subject is cut off at the edge of the image or is small, or there are several separate objects,
        "Notes about the subject" appears in the image field (generation is not stopped).
      - For text, "Template" under the text field inserts an example sentence or a prefix with conditions.
        The generation AI reads up to 77 tokens, including the two start and end markers (roughly 50 to 60 English
        words); an estimate of the token count and, when the text is too long, the part that is not used are shown.
        If the text contains words with non-English characters, or full-width letters, digits or symbols
        (including （ ） ～), a warning asks you to fix them. Without colors, the body tends to come out white or
        gray, so also write the colors of the hair, clothes, shoes and so on in English.
  (2) Preview: Rotate and inspect the generated model and the male and female results. If the front faces backwards,
      fix it with "Rotate 180° and reconvert". The "Candidates" tab of the preview lists the candidates compared by
      the automatic selection (arm angle, color, reason for exclusion, possible weapon); choose another candidate and
      remake the model with "Use this candidate".
      If you stopped during the conversion, "Use this candidate" in the row of the current body redoes only the
      conversion.
  (3) Give the equipment a name (English and Japanese) and click "Save as equipment" to add it to the Library.
      For the English name, ASCII letters, digits and symbols are recommended (the game's fonts in English have only
      ASCII characters, so Japanese text and characters such as é will probably not be shown; a warning is shown
      when you save).
  (4) MOD: In "MOD" on the left, click "Build MOD" → "Install to Grim Dawn".
      Before installing, the app checks that the game is not running and checks for conflicts with files of other
      MODs, and it backs up the original files.
      "Uninstall" returns to the state before the first install.
      If you close the app during an install, uninstall or similar operation, the app does not cancel it partway;
      it waits for the operation to finish and then closes.
      The MOD includes name files for the game's 13 languages (Text_XX.arc) (Japanese names for the game in
      Japanese; the Japanese names and descriptions were confirmed in the game; whether the game reads them when it
      is set to a language other than English and Japanese has not been confirmed).
  (5) In the game: set the Steam launch option /basemods, start Grim Dawn (chapter 4), and do the following in order.
        1. Buy "Illusion: <name>" from the Quartermaster in Devil's Crossing (Ellis; Survivors reputation of Friendly
           or higher), and right-click it to add it to your collection (close the shop first: right-clicking an item
           in your inventory while the shop is open sells it back).
        2. Equip something on the head, chest, shoulders, hands, legs and feet (illusions can only be applied to
           equipped slots).
        3. At the Illusionist, apply your equipment's illusion to the head and invisible (transparent) illusions to
           the other slots. If you wear a medal, applying this MOD's invisible illusion to the medal as well
           should hide the badge on the chest (not confirmed in the game).

  Other features
    - GLB files made with other tools can also be converted from the "GLB file" input (no GPU needed).
      For a GLB that references texture images or .bin as external files, only files referenced by a relative path
      from the GLB's folder are loaded (for a GLB that references a file by an absolute path, on another drive, in a
      network location, etc., the conversion stops without reading that file).
    - "Reconvert" in the Library rebuilds saved equipment with the current version's conversion without redoing the
      3D generation (for when an app update improves the conversion; the internal ID and in-game records do not
      change, so illusions already obtained in the game should keep working as the same items, but this has not
      been confirmed in the game).
      After rebuilding, run "Build MOD" and "Install to Grim Dawn" again.
      A check box in the confirmation window also lets you reconvert with the front rotated 180° (or back to the
      direction before the rotation) (the box is unchecked by default).
    - "Deleted equipment…" in the Library restores deleted equipment with the same internal ID (then build and
      install the MOD; whether illusions already obtained in the game keep working as the same items has not been
      confirmed in the game).
    - "Consumable text…" in the Library changes the name and description (English and Japanese) of the consumable
      bought from the Quartermaster (blank means the default text; ASCII letters, digits and symbols are recommended
      in the English fields; the game wraps descriptions only at half-width spaces, so put a half-width space
      between phrases in Japanese descriptions, as the default text does).
    - "Export…" and "Import file…" in the Library let you exchange your equipment with other users (.gdskin).
    - For multiplayer, you can also make a share file (.gdskinmod) that hands over your MOD as is
      (multiplayer has not been tested). If the verification code on the "MOD" page (the code for
      the whole MOD: 12 characters made from the names and SHA-256 hashes of all files, for example 1A2B-3C4D-5E6F)
      is the same for everyone, everyone has the same MOD. If the codes differ, "Verification details" shows which
      files differ (the database, or only the appearance and names), and you can also enter the other person's
      verification code to compare. The code for database.arz alone (the database code) is shown separately.
      If the contents of a received MOD differ from what this tool creates, the confirmation screen shows a warning
      for each item (these differences alone do not stop the import). Do not use files received from people you do
      not know.

  Advanced settings and additional features (all off by default)
    Generate → "Generation settings" → "Advanced settings"
    - Sampling: Standard (the model's default, 25 steps) / Fast (12 steps; for image input, the settings of the
      official TRELLIS demo; about half the time per candidate; the shape and detail change even with the same seed)
    - Low VRAM mode: unloads models that are no longer needed from GPU memory while the mesh is being built (peak use
      drops by about 3.5 to 4.5 GB, and each candidate takes about 2 seconds longer; measured on a 24 GB GPU)
    - Block network access: during generation, refuses outgoing connections from the generation process (Python) and
      writes the attempted destinations to the log (connections that native code opens by itself are not covered.
      Generation does not use the network anyway; this setting makes sure of it)
    - MOD textures: the maximum size of the textures made by the conversion: 2048 (default) / 1024 / 512
      (how equipment made with 1024 or 512 looks in the game has not been confirmed)
    - Keep candidates: keeps the generation result of each candidate in the draft, so that "Use this candidate" and
      "Rebuild GLB" under the preview work without redoing the 3D generation. Uses about 45 MB per body (about 180 MB
      for 4 candidates, and twice that if the candidate selection reaches its second round), and it is not included
      in equipment or share files. The generation results are kept until the draft is saved or discarded, and are
      deleted then. However, they are kept when "Keep intermediate generation files (drafts) after saving" in
      Settings is on, and for drafts whose conversion failed (failed drafts can be deleted from "Data usage and
      cleanup" in Settings).
    - Background removal: removes the background in the app before generation and lets you choose post-processing
      (smoothing the mask, removing background color left at the edges, alpha matting). The effect depends on the
      image, so compare the results with "Check background removal" (the effect has not been evaluated yet).
    - Appearance remake (experimental): shows "Remake appearance from text…" in the Library. Remakes only the colors,
      materials and decorations from English text while keeping the shape (pose) of saved equipment, and saves the
      result as new equipment (requires the optional text → 3D components and acceptance of the CLIP notice. Tested
      with only 1 sample and 2 texts; the colors may not match the text, and small details of the shape may change).
      When you export equipment with a remade appearance to an equipment package (.gdskin), the internal ID of the
      original equipment and the SHA-256 of the original 3D model are included in the record.
    Generate → "Advanced settings for this image" in the image field
    - Force background removal: even for an image that is almost entirely opaque (such as a screenshot with only
      transparent corners), fills the transparent areas with white and then removes the background (requires u2net)
    3D preview
    - Front marker: shows an arrow on the floor and the label "Front (+Z)" at the in-game front (+Z) (depending on the
      window width, the check box appears at the right end of the model selection row or to the right of
      "Wireframe"; only while a 3D model is shown)
    Settings → "Display language"
    - English: English screens (the default is Japanese). Choose English and click "Save settings"; the change takes
      effect after you restart the app (the backend restarts with the app). On the Japanese screens, the setting is
      表示言語（Language） and the button is 設定を保存. All text the app shows is then in English, including
      messages from the backend and from the generation and conversion processes, error "Details", the environment
      check, the setup items and their license information, the mods folder table and newly written log lines; the
      CLIP notice is also shown in English. What stays in Japanese: log lines written before the switch (they stay
      in the language they were written in) and text you typed (such as names). Error messages from Windows in
      the error "Details" (such as "The system cannot find the file specified") are in the display language of
      Windows. The developer tools (the command-line tools gdconvert and modbuilder, and the packaging and
      publishing scripts) and the multiplayer check steps (docs/ in the source repository) are in Japanese only. The
      English wording follows the game's official English terms but has not been reviewed by native English
      speakers.
      Even when the app cannot connect to the backend, the display language and the Python location can be saved.
    The values of the advanced generation settings are saved as the defaults for next time when you generate, and
    are recorded in the draft and the equipment. "Regenerate" in the Library restores Sampling,
    Background removal, MOD textures and each image's Force background removal setting to the recorded values
    (Low VRAM mode and Block network access keep their current settings).

  Other items in Settings (they run only when you click them)
    - Data usage and cleanup: Shows the size of each item in the data folder and what cleanup can delete.
      "Delete old builds…", "Delete old install backups…", "Permanently delete deleted equipment…",
      "Delete saved input images…" and "Delete failed, interrupted or saved drafts…" show what will be deleted and
      what will be kept (with reasons) in a confirmation window, and delete after you check the box. Deleted items cannot be
      restored. The following are never deleted: the installed build, the build of the state before the current
      install (the state that "Remove the received MOD and restore" returns to), the latest successful build, the
      backups used for uninstalling and for returning to the state before the current install, the 5 newest backups,
      drafts being processed or shown, and the input images used in the image field of the Generate page.
      To regenerate equipment whose input image was deleted, select the image again.
      Nothing is deleted automatically (drafts left in progress are only marked "interrupted" at startup, not
      deleted).
    - TRELLIS runtime management (re-verify, reinstall, remove): Only destinations created by the app's setup are
      covered. "Re-verify models…" re-reads every file with SHA-256 and downloads again from the source only the
      files that do not match. "Reinstall Python and packages…" downloads Python, PyTorch and the packages again from
      their sources, replaces them and runs a self-test (up to about 4.2 GB is downloaded).
      "Remove the text → 3D models…" and "Remove the whole TRELLIS runtime…" remove only what the app downloaded
      (the Library, built MODs and settings are not removed).


4. Steam launch option /basemods
--------------------------------

  The MOD that this tool makes uses the "/basemods" method (it is loaded in the main campaign).
  Set /basemods in the Steam launch options. This tool does not change the launch options automatically.

    1. In the Steam library, right-click Grim Dawn → "Properties"
    2. Under "General" → "Launch Options", enter /basemods
       (if there are other options, add it at the end, separated by a space)
    3. Start Grim Dawn from Steam

  Notes
    - The app's launch option check (in "Environment check" in Settings, in "5. Launch options" of the setup and on
      the MOD page) reads the settings of the user currently logged in to Steam (if that cannot be determined, the
      user who logged in last) (it only reads them). Right after you set the option while Steam is running, the app
      may report that /basemods is not set until you exit Steam. Exit Steam, then click "Check again" (on the MOD
      page, "Refresh").
    - Only one mods\database.arz is read, so this MOD cannot be used together with other main-campaign MODs.
    - If you will not use other /basemods MODs after uninstalling, remove /basemods from the launch options.
    - Back up your saves: Before you install a MOD for the first time and before you remove the MOD, it is
      recommended to copy the save folder (Documents\My Games\Grim Dawn\save; if you use Steam Cloud, also
      Steam\userdata\<number>\219990\remote\save) as a whole to another location. What happens to this tool's
      consumables and collected illusions when you start the game without /basemods, or load the character in the
      Crucible or a Custom Game, has not been tested.


5. Troubleshooting
------------------

  More detailed steps are in the app's "Help" page under "Troubleshooting".

  * "Windows protected your PC" appears when you start the app
      - This SmartScreen window appears because the app is not signed. You can start the app with the steps in
        chapter 2 (2) ("More info" → "Run anyway").
        Check the SHA-256 of the ZIP first with the steps in chapter 2 (0).
      - Antivirus software may block (quarantine) GDSkinStudio.exe or python\python.exe.
        If you extracted them from a ZIP whose SHA-256 matches the one on the download page, consider whether to add
        the extracted folder to that software's exceptions (exclusions).

  * "The app is already running" appears
      - Only one instance of the app can run with the same data folder. Use the window that is open.
      - If the previous app is still closing, wait a moment and start it again (if a message says that another
        backend is running with the same data folder, wait a moment and click "Restart backend").

  * The app does not start / "The backend … has stopped" appears
      - Check that you extracted the ZIP before starting the app (it does not work from inside the ZIP).
      - Check that you did not delete files in the extracted folder (such as the python\ folder).
      - Check that antivirus software is not blocking python\python.exe.
      - Logs: app-<date>.log and backend-<date>.log in %LOCALAPPDATA%\GrimDawnSkinMOD\logs\
        (if the backend stopped after starting, the end of the backend's output at that time is kept in
        app-<date>.log and in the ERROR lines on the Log page)
      - If "Could not start Python (python.exe) for running the backend" appears, check "Backend Python" in Settings.

  * "Cannot use the data folder" appears at startup and the app exits
      - The data folder (the location given with --data-dir or the GDSKIN_HOME environment variable, or the default
        location if none is given) or the logs\ folder inside it cannot be created, or the path is malformed.
        Check the path you specified, the drive and the access permissions, then start the app again.

  * The setup download fails
      - Click "Retry (continue)". Files already downloaded are verified and reused.
        If a connection to huggingface.co or another site was cut partway, retrying also continues the download from
        where it stopped.
      - The download fails if connections to github.com (including the redirect targets codeload.github.com and
        release-assets.githubusercontent.com), huggingface.co (including the delivery servers it redirects to, such
        as us.aws.cdn.hf.co), releases.astral.sh, download.pytorch.org, pypi.org, files.pythonhosted.org,
        dl.fbaipublicfiles.com or nvidia-kaolin.s3.us-east-2.amazonaws.com are blocked, for example on a company or
        school network.
      - "The source did not return the file (HTTP <number>)": the file at the source may have been moved or removed,
        or a proxy or filter may be blocking it. If it keeps happening, check for a new version of the app.
      - "Could not write to a file being downloaded": another app or antivirus software may have the file open.
        Wait a moment and retry.
      - "Not enough free space": change the destination to a drive with more free space.
      - "The destination path is too long": use a short path of 80 characters or fewer.

  * "The Visual C++ Redistributable is required" (setup self-test)
      - Install the latest Visual C++ Redistributable (x64) from https://aka.ms/vc14/vc_redist.x64.exe
        (administrator rights are required), then click "Retry (continue)". Files already downloaded are reused.

  * "No GPU found" (or "No supported GPU found"), "The GPU driver is too old", "Not enough GPU memory"
      - Update the NVIDIA driver to R570 or later.
      - GPUs with less than 12 GB of GPU memory or with compute capability below 6.0 cannot be used for 3D
        generation. Close other apps (such as games) before generating.
      - If the GPU has less than 16 GB of memory, or if generation fails for lack of memory, try "Low VRAM mode" in
        the advanced generation settings.

  * "Could not load the models" / the setup self-test fails
      - Check the "Details" and the log.
      - If the model files may be damaged, use "Re-verify models…" in "TRELLIS runtime management" in Settings
        (it re-reads every file and downloads again only the files that do not match).
      - If Python or the packages may be damaged, use "Reinstall Python and packages…".
      - Both cover only destinations created by the app's setup.

  * The re-verify result says "Download them again from the Setup page"
      - Some files did not match, but they were not downloaded again because there is no record of acceptance of the
        licenses and notices. Run "Setup" on the left; it downloads again only the files that do not match.

  * An operation in "TRELLIS runtime management" in Settings fails and the hint mentions "Retry (continue)"
      - That button is on the Setup page, not in Settings (a limitation of this app). Fix the cause, then click
        the same button in Settings again (such as "Reinstall Python and packages…").

  * "A file needed for generation is missing"
      - Files are not downloaded during generation, so generation was stopped. Run "Setup" on the left
        (it downloads the missing files and verifies and reuses the files already downloaded).
      - If files may be damaged, use "Re-verify models…".

  * "Reference meshes are missing"
      - Prepare them with "Prepare reference meshes" in the error window or in "4. Reference meshes" of the setup.
      - If Grim Dawn is not found, specify the Grim Dawn install folder in Settings.

  * "The reference meshes are corrupted" / "Cannot open the reference meshes"
      - If they are corrupted, extract them again with "Prepare reference meshes" in the error window or
        "Prepare from the game" in Settings.
      - If they cannot be opened (another app is using them, or you do not have permission to read them),
        close or pause antivirus, backup or sync software (OneDrive, etc.), or check that you have
        permission to read refs\ in the data folder, then try again ("Prepare from the game" does not fix
        this; it skips files it cannot open).

  * Part of the character disappears / the background remains in an image with a background
      - Check with "Check background removal"; if it does not work well, use a PNG with a transparent background.
      - You can also compare the "Background removal" advanced setting (background removal in the app with
        post-processing) using "Check background removal".

  * The background becomes part of the 3D model (a board or a stand) / "…almost all of it was opaque, so it was
    generated without removing the background"
      - The background is not removed from an image that is almost entirely opaque, such as a screenshot with only
        transparent corners. In the image field, turn on "Advanced settings for this image" →
        "Force background removal" and generate again, use a PNG with a transparent background, or save the image
        again as a JPEG.

  * The feet or shins are distorted or torn in the game (more likely with bodies standing with the legs together)
      - For equipment converted with an earlier version of the app, select the equipment in the Library and click
        "Reconvert", then run "Rebuild the MOD" → "Install to Grim Dawn".
      - If they are still distorted, try another seed with "Regenerate", or use an image of a standing pose with the
        arms and legs slightly apart.

  * "The MOD build stopped partway"
      - The mods folder was not changed. The "Details" in the error window show the cause, such as the output of the
        game's bundled tools.

  * "The game's bundled tool is missing" / "Environment check" in Settings shows ArchiveTool.exe or
    TextureCompiler.exe as "not found"
      - You can restore them in the Steam library: Grim Dawn's Properties → "Installed Files" →
        "Verify integrity of game files".

  * Installing or uninstalling failed
      - If writing fails, the mods folder is returned to its state before the operation.
      - If a message says that some files could not be restored, close Grim Dawn and any programs that have the
        files open, then do the same operation again (the files from before the operation are backed up in
        install\backups\<ID> in the data folder).
      - If the mods folder contains read-only files, the operation stops at the check before any change and tells
        you (clear "Read-only" in the file's properties in File Explorer).
      - If "Could not save the install record" appears, the mods folder has been changed, but the install record
        (install\ in the data folder) is out of date. As when the connection to the backend is lost partway, check
        the current state in the "mods folder status" table on the "MOD" page.
      - If the completion message says there are warnings ("Done (with warnings)"), read the warnings ("Could not
        update the install status in the equipment library" means that the changes to the mods folder are complete,
        but marks such as "Installed" in the Library may be out of date; the log has the reason).

  * The 3D preview says "The 3D preview cannot be shown on this PC (WebGL is not available…)"
      - Updating the graphics driver may help. You can also check the conversion result on the
        "Conversion check image" tab of the preview.

  * After a game update (patch) / a warning banner appears on the "MOD" page
      - The MOD contains a copy of the Quartermaster's stock (official records), so when a game update changes the
        stock, the MOD overwrites it with the old contents.
      - If the banner lists changed tiers, rebuild with "Rebuild the MOD" ("Build MOD"), then install again with
        "Install the latest build" ("Install to Grim Dawn"). If the latest build already matches the current game,
        installing it again is enough.
      - The "Game version" row shows the Steam build numbers (when the MOD was built and now) and one of the
        following: the same version; updated, but the stock has not changed, so the MOD can be used as is; a MOD
        that matches the current game is needed; or not compared because there is no record.
      - If you use a received MOD, ask the person who made it to rebuild it with the current game and share it
        again, then receive and install it again. If you replace it with your own MOD, your verification code will
        no longer match theirs.

  * An old AI Skin MOD remains after uninstalling
      - Uninstalling returns to "the state before the first install". If the mods folder contained an AI Skin MOD
        with no record in this app (for example, a prototype) at that time, the folder returns to that MOD, and the game
        loads it (the MOD page and the confirmation window show this). If you do not need it, exit the game, open
        the folder with "Open mods folder", and move those files somewhere else.

  * Your verification code does not match the other person's
      - Check which files differ in "Verification details" on the "MOD" page. If database.arz is the same and only
        the appearance (Items.arc) and names (Text_XX.arc) differ, you are expected to be able to connect, but each
        of you sees different appearances or names for the equipment.

  * The illusion does not appear in the game
      - Check that the Steam launch options include /basemods (chapter 4).
      - Check that the MOD is installed on the "MOD" page. It cannot be used together with other /basemods MODs
        (also check for "Other file (loaded)" in the table on the MOD page).
      - Illusions can only be applied to equipped slots. Check that you have something equipped on the head, chest,
        shoulders, hands, legs and feet (chapter 3 (5)).

  * Item names and descriptions are not shown correctly when the game is set to a language other than English
      - The MOD includes name files for 13 languages (Text_XX.arc) (Japanese names for Japanese, English names for
        the other languages). The Japanese names and descriptions were confirmed in the game set to Japanese
        (2026-11-18). Whether the game reads them when it is set to a language other than English and Japanese has
        not been confirmed in the game yet.

  * Some text is still in Japanese on the English screens
      - A change of display language takes effect after you restart the app (until then, the previous language is
        shown).
      - Log lines written before the switch stay in the language they were written in. Text you typed (such as
        names) is shown as you typed it.
      - Error messages from Windows in the error "Details" are in the display language of Windows.
      - The developer tools (the command-line tools gdconvert and modbuilder, and the packaging and publishing
        scripts) and the multiplayer check steps (docs/ in the source repository) are in Japanese only.
      - If you find Japanese text anywhere else, please let us know.

  * Equipment made while the app was started from inside Claude or another app does not appear when you start the
    app normally
      - When the app is started from inside some apps, the data folder may be split off into a different location
        (a virtualized folder). Start the app normally, for example from File Explorer. You can import the equipment
        with "Import…" in the Library.


6. Uninstalling (removal)
-------------------------

  (1) Remove the MOD installed in the game: Click "Uninstall" on the app's "MOD" page
      (this returns to the state before the first install). Also remove /basemods from the Steam launch options if
      needed. If there was an old AI Skin MOD with no record in this app (for example, a prototype) before the first
      install, the folder returns to that MOD (the screen shows this; chapter 5).
  (2) Remove the TRELLIS runtime (optional): Before you remove the app, you can remove the runtime and models the app
      downloaded (about 13 GB, or about 19 GB with the text → 3D items) with "Remove the whole TRELLIS runtime…" in
      "TRELLIS runtime management" in Settings (only destinations created by the app's setup are covered; other
      files you placed there are kept).
  (3) Remove the app: Delete the extracted folder as a whole (nothing is registered in the registry or the Start menu).
  (4) Remove the data (optional): Always do the uninstall in (1) first. If the install replaced files of another MOD,
      the original files exist only in install\backups in the data folder, and deleting the data folder makes it
      impossible to restore them. Then delete the following folder. This removes your equipment, MOD builds, install
      records and backups, settings, logs, and the TRELLIS runtime and models (if they are in the default location).
        %LOCALAPPDATA%\GrimDawnSkinMOD
      If the runtime destination is somewhere else (a location you chose during the setup, or
      %SystemDrive%\GDSkinStudio\trellis-runtime, which becomes the default when your user folder name contains
      full-width characters, etc.), do (2) or delete that folder as well.
  Note: To replace the app with a new version, do only (3) and extract the new ZIP.
    The data folder is kept, so your equipment and downloaded models can be used as they are.

  If the app cannot remove the MOD (no install record)
    If you deleted the data folder, or installed with a data folder in another location (such as the virtualized
    folder case in chapter 5), "Uninstall" in the app cannot be clicked (if you can still click it, it stops with
    "No MOD has been installed by this app"), and the files in the mods folder remain.
      1. If you do not use other /basemods MODs, remove /basemods from the Steam launch options
         (this alone stops the game from loading the files directly in the mods folder).
      2. To delete the files as well, exit Grim Dawn and check how the "MOD" page of the app classifies the files in
         the mods folder. Delete only the database.arz, Items.arc and Text_XX.arc files directly in the mods folder
         that are shown as "Installed by this app" or "AI Skin MOD with no record" (Text_CS.arc to Text_ZH.arc for
         13 languages; MODs made with earlier versions have only Text_EN.arc and possibly Text_JA.arc). Do not
         delete files shown as "Another MOD or unknown file". Do not delete folders such as mods\survivalmode (the
         Crucible DLC) either.
    It is recommended to back up your game saves before you remove the MOD (chapter 4).


7. Folder layout and where data is stored
-----------------------------------------

  Extracted folder (the app itself; replace it as a whole when updating)
    GDSkinStudio.exe        The app (.NET 10, self-contained; no .NET installation needed)
    web\                    3D preview (three.js)
    python\                 Python 3.11.9 (embeddable package) and the libraries for conversion and MOD builds
    app\backend\            Backend (processing for generation, conversion and MOD builds)
    src\                    Programs for conversion (gdconvert, gdmesh) and MOD builds (modbuilder)
    scripts\trellis\        3D generation scripts and the definitions of what the setup downloads (versions, hashes)
    licenses\               Full license texts
    README.txt              This document (Japanese)
    README.en.txt           This document (English)
    THIRD_PARTY_NOTICES.txt Third-party notices and licenses (Japanese)
    THIRD_PARTY_NOTICES.en.txt  Third-party notices and licenses (English)
    LICENSE.txt             The license of this tool itself (MIT License)
    MANIFEST.txt            List of contents (size and SHA-256 of each file)

  Data folder (kept when the app is updated; can be changed with --data-dir): %LOCALAPPDATA%\GrimDawnSkinMOD
    library\                Your equipment
    work\                   Generation drafts (generation and conversion results before saving as equipment)
    shared\                 Received MODs (contents of the .gdskinmod share files you imported)
    builds\                 MOD builds
    install\                Install records and backups of files from before installing (including the original
                            files of other MODs that were replaced)
    refs\                   Reference meshes (extracted from your game)
    trellis-runtime\        TRELLIS runtime and models (the default destination)
    logs\                   Logs (deleted automatically after 14 days; they do not contain the contents of the
                            images or text you enter)
    app\                    Settings, setup records
    You can check the size of each item and clean up in "Data usage and cleanup" in Settings.

  %SystemDrive%\GDSkinStudio\trellis-runtime
    The default destination when the data folder cannot be used, for example because your user folder name contains
    full-width characters

  <Grim Dawn>\mods\ (the installed MOD; uninstalling returns it to the state before the first install)
    database.arz, Items.arc, Text_XX.arc for 13 languages


8. Licenses
-----------

  - This tool itself (the GD Skin Studio program): MIT License.
      {copyright}
    The full text is in LICENSE.txt. When redistributing, include this copyright notice and the text of LICENSE.txt.
    You can also check it in "About" in the app's "Settings".
  - Bundled third-party software (.NET, WebView2 SDK, three.js, Python, numpy, scipy, Pillow, lz4,
    fast-simplification, xatlas): not covered by the MIT License; each is under its own license.
    See THIRD_PARTY_NOTICES.en.txt (Japanese: THIRD_PARTY_NOTICES.txt) and licenses\bundled\.
  - What the first-run setup downloads (not bundled): the setup screen and Settings show the license name and full
    text of each item (the same files are also in licenses\setup\).
    Besides plyfile under GPL-3.0 or later, kaolin 0.18.0 includes kaolin/non_commercial, which is under the NVIDIA
    Source Code License (non-commercial, research or evaluation purposes only). The setup screen shows a mark and a
    note, and THIRD_PARTY_NOTICES.en.txt shows "Note:".
  - Grim Dawn files are not bundled. The reference meshes and the MOD template are read from the user's game.
