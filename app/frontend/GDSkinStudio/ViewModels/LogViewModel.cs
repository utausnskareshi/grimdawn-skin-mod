using System.ComponentModel;
using System.Windows;
using System.Windows.Data;
using System.Windows.Input;
using GDSkinStudio.Services;
using GDSkinStudio.Views;

namespace GDSkinStudio.ViewModels;

/// <summary>ログの表示（画面の操作・ジョブの進捗の一覧と、バックエンドのログファイルの末尾）。</summary>
public sealed class LogViewModel : ObservableObject
{
    private readonly MainViewModel _main;
    public ICollectionView Lines { get; }

    private bool _onlyProblems;
    public bool OnlyProblems { get => _onlyProblems; set { if (Set(ref _onlyProblems, value)) Lines.Refresh(); } }

    private string _backendText = "";
    public string BackendText { get => _backendText; private set => Set(ref _backendText, value); }
    private string _logDir = "";
    public string LogDir { get => _logDir; private set => Set(ref _logDir, value); }

    public ICommand RefreshCommand { get; }
    public ICommand OpenFolderCommand { get; }
    public ICommand CopyCommand { get; }

    public LogViewModel(MainViewModel main)
    {
        _main = main;
        Lines = CollectionViewSource.GetDefaultView(AppLog.Lines);
        Lines.Filter = o => !OnlyProblems || o is LogLine { IsError: true } or LogLine { IsWarning: true };
        RefreshCommand = new AsyncCommand(RefreshAsync, () => _main.IsReady);
        OpenFolderCommand = new RelayCommand(() => Dialogs.OpenFolder(App.Paths.LogsDir));
        CopyCommand = new RelayCommand(() =>
        {
            try { Clipboard.SetText(BackendText); } catch (Exception) { }
        });
        LogDir = App.Paths.LogsDir;
    }

    public async Task RefreshAsync()
    {
        var c = _main.Client;
        if (c == null) return;
        var r = await c.GetAsync("logs?lines=400");
        BackendText = string.Join("\n", r.Strings("lines"));
    }
}
