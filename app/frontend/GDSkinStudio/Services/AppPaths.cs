using System.IO;

namespace GDSkinStudio.Services;

/// <summary>
/// アプリ本体・バックエンド・データフォルダーの場所（docs/app_architecture.md 4 章・5.1 節）。
/// データフォルダーは起動引数 --data-dir、環境変数 GDSKIN_HOME、既定 %LOCALAPPDATA%\GrimDawnSkinMOD の順。
/// </summary>
public sealed class AppPaths
{
    public string DataHome { get; }
    public string AppBase { get; }
    public string WebRoot { get; }
    public string? BackendDir { get; }
    public string? RepoRoot { get; }

    public string UiSettingsPath => Path.Combine(DataHome, "app", "ui_settings.json");
    public string LogsDir => Path.Combine(DataHome, "logs");
    public string WebViewDataDir => Path.Combine(DataHome, "webview");

    public AppPaths(string? dataDirArg)
    {
        DataHome = Path.GetFullPath(
            !string.IsNullOrWhiteSpace(dataDirArg) ? dataDirArg :
            !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("GDSKIN_HOME")) ? Environment.GetEnvironmentVariable("GDSKIN_HOME")! :
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GrimDawnSkinMOD"));
        AppBase = AppContext.BaseDirectory;
        WebRoot = Path.Combine(AppBase, "web");
        // 配布時: <アプリ>\backend\run_backend.py。開発時: 上のフォルダーをたどってリポジトリの app\backend を探す
        var dist = Path.Combine(AppBase, "backend", "run_backend.py");
        if (File.Exists(dist))
        {
            BackendDir = Path.GetDirectoryName(dist);
            RepoRoot = AppBase;
        }
        else
        {
            var d = new DirectoryInfo(AppBase);
            while (d != null)
            {
                var cand = Path.Combine(d.FullName, "app", "backend", "run_backend.py");
                if (File.Exists(cand))
                {
                    BackendDir = Path.GetDirectoryName(cand);
                    RepoRoot = d.FullName;
                    break;
                }
                d = d.Parent;
            }
        }
    }
}
