using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using GDSkinStudio.Localization;

namespace GDSkinStudio.Services;

public sealed class BackendStartException(string message, string? detail) : Exception(message)
{
    public string? Detail { get; } = detail;
}

/// <summary>
/// バックエンド（python -I run_backend.py）の起動と終了（docs/app_architecture.md 3.4 節・4 章）。
/// 起動ごとに合言葉を作って環境変数で渡し、標準出力の READY 行でポートを知る。プロセスは Job Object に入れる
/// （バックエンド自身も起動の直後に自分用の Job に入り、作業プロセスを残さない。run_backend.py）。
/// </summary>
public sealed class BackendHost : IDisposable
{
    /// <summary>終了の要求の後に待つ秒数。バックエンドの wait_idle（10 秒）より長くする（2026-10-17 監査 C99）。</summary>
    public static readonly TimeSpan ExitWait = TimeSpan.FromSeconds(12);
    /// <summary>実行中の取り消せないジョブ（導入など）があるときに待つ秒数。バックエンドの上限（60 秒）より長くする。</summary>
    public static readonly TimeSpan ExitWaitUncancellable = TimeSpan.FromSeconds(65);
    /// <summary>利用者が「それでも閉じる」を選んだときに待つ秒数。</summary>
    public static readonly TimeSpan ExitWaitForced = TimeSpan.FromSeconds(3);
    /// <summary>同じデータフォルダーで別のバックエンドが動いているときの終了コード（run_backend.py の EXIT_LOCKED）。</summary>
    private const int ExitLocked = 3;

    private readonly AppPaths _paths;
    private readonly Func<string> _pythonPath;
    private Process? _proc;
    private JobObject? _job;
    private readonly Queue<string> _stderrTail = new();
    private volatile bool _stopping;
    private int _generation;        // 起動ごとの番号。差し替えた古いプロセスの Exited・標準エラーを無視する（C106）

    public string Token { get; private set; } = "";
    public int Port { get; private set; }
    public bool IsRunning => _proc is { HasExited: false };
    public string? PythonUsed { get; private set; }

    /// <summary>起動した後（READY の後）に予期せずに終了した（引数は標準エラーの末尾）。今のプロセスのときだけ（再起動で
    /// 差し替えた古いプロセスは通知しない）。起動の途中の終了は通知せず、StartAsync の例外（BackendStartException）で返す。</summary>
    public event Action<string>? Exited;

    public BackendHost(AppPaths paths, Func<string> pythonPath)
    {
        _paths = paths;
        _pythonPath = pythonPath;
    }

    public string StderrTail()
    {
        lock (_stderrTail) return string.Join("\n", _stderrTail);
    }

    public static string? FindPython(string configured)
    {
        var cands = new List<string>();
        // エクスプローラーの「パスのコピー」で付く前後の引用符は取り除く（Windows のパスに '"' は使えない）
        configured = (configured ?? "").Trim().Trim('"');
        if (configured.Length > 0)
        {
            if (File.Exists(configured)) return configured;
            // 黙って次の候補に移ると、設定を直したつもりでも別の Python が使われていることが分からない（R17B-02）
            AppLog.Warn(L.F("Log_Backend_PythonNotFound", configured));
        }
        cands.Add(Path.Combine(AppContext.BaseDirectory, "python", "python.exe"));        // 配布物（フェーズ6）
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        cands.Add(Path.Combine(local, "Programs", "Python", "Python311", "python.exe"));
        foreach (var c in cands)
            if (File.Exists(c)) return c;
        // py ランチャー
        var o = QueryOutput("py", "-3.11 -c \"import sys;print(sys.executable)\"", PyQueryLimit);
        if (!string.IsNullOrEmpty(o) && File.Exists(o)) return o;
        return null;
    }

    /// <summary>py ランチャーへの問い合わせを待つ上限。</summary>
    internal static readonly TimeSpan PyQueryLimit = TimeSpan.FromSeconds(10);

    /// <summary>
    /// プログラムを起動して標準出力（前後の空白を除く）を返す。上限までに終わらない・起動できないときは null（終わらないものは
    /// 子・孫ごと終了させる）。以前は ReadToEnd を先に呼んだので、py や起動された Python が止まると上限が効かず、起動中のまま
    /// 固まった（2026-10-27 R17A-03）。
    /// </summary>
    internal static string? QueryOutput(string file, string arguments, TimeSpan limit)
    {
        try
        {
            var psi = new ProcessStartInfo(file, arguments)
            { UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true };
            using var p = Process.Start(psi);
            if (p == null) return null;
            var read = p.StandardOutput.ReadToEndAsync();
            // 孫プロセスが標準出力を持ったままのときは、子が終わっても読み終わらないので、読み取りにも上限を付ける
            if (!p.WaitForExit(limit) || !read.Wait(TimeSpan.FromSeconds(2)))
            {
                AppLog.Warn(L.F("Log_Backend_QueryTimeout", file + " " + arguments, limit.TotalSeconds));
                try { p.Kill(entireProcessTree: true); } catch (Exception) { }
                return null;
            }
            return read.Result.Trim();
        }
        catch (Exception) { return null; }
    }

    /// <summary>
    /// venv の python.exe（本物の Python を子として起動する転送用の実行ファイル）か。同じフォルダーかその 1 つ上に
    /// pyvenv.cfg がある（venv の転送用の実行ファイルと同じ探し方）。
    /// </summary>
    public static bool IsVenvPython(string python)
    {
        try
        {
            var dir = Path.GetDirectoryName(Path.GetFullPath(python));
            if (dir == null) return false;
            if (File.Exists(Path.Combine(dir, "pyvenv.cfg"))) return true;
            var up = Path.GetDirectoryName(dir);
            return up != null && File.Exists(Path.Combine(up, "pyvenv.cfg"));
        }
        catch (Exception) { return false; }
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_paths.BackendDir == null)
            throw new BackendStartException("backend_not_found", "app\\backend\\run_backend.py");
        // Python を探すのは UI スレッドの外で（設定の場所がつながらないネットワーク上のときの File.Exists・py ランチャーへの
        // 問い合わせで、窓が「応答なし」にならないように。2026-10-27 R17A-03）。設定の値は呼び出し元のスレッドで読む
        var configured = _pythonPath();
        var py = await Task.Run(() => FindPython(configured), ct) ?? throw new BackendStartException("python_not_found", null);
        PythonUsed = py;
        if (IsVenvPython(py))
            // 本物のバックエンドが下の Job に入る前に子を作りうる（2026-10-17 監査 C107）。バックエンドは自分用の Job に
            // 入るので作業プロセスは残らないが、本体の python.exe を指定するほうが確実（自動では置き換えない）
            AppLog.Warn(L.T("Log_Backend_VenvPython"));
        Token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        int gen = Interlocked.Increment(ref _generation);
        _stopping = false;
        lock (_stderrTail) _stderrTail.Clear();

        var psi = new ProcessStartInfo(py)
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = false,
            CreateNoWindow = true,
            WorkingDirectory = _paths.BackendDir,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in new[] { "-I", "-X", "utf8", Path.Combine(_paths.BackendDir, "run_backend.py"),
                                  "--parent-pid", Environment.ProcessId.ToString() })
            psi.ArgumentList.Add(a);
        psi.Environment["GDSKIN_BACKEND_TOKEN"] = Token;
        psi.Environment["GDSKIN_HOME"] = _paths.DataHome;
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        // 表示言語（"ja" / "en"）。バックエンドと子プロセスの文・ログをこの言語にする（D-060。表示言語の変更は再起動の後に効く）
        psi.Environment["GDSKIN_LANG"] = GDSkinStudio.Localization.L.Language;
        psi.Environment.Remove("PYTHONPATH");
        psi.Environment.Remove("PYTHONHOME");

        var ready = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        bool Current() => gen == Volatile.Read(ref _generation);
        proc.OutputDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            if (e.Data.StartsWith("GDSKIN_BACKEND_READY ", StringComparison.Ordinal))
            {
                try
                {
                    var j = JsonNode.Parse(e.Data["GDSKIN_BACKEND_READY ".Length..])!;
                    ready.TrySetResult((int)j["port"]!);
                }
                catch (Exception ex) { ready.TrySetException(ex); }
            }
        };
        proc.ErrorDataReceived += (_, e) =>
        {
            if (e.Data == null || !Current()) return;
            lock (_stderrTail)
            {
                _stderrTail.Enqueue(AppLog.Redact(e.Data));
                while (_stderrTail.Count > 60) _stderrTail.Dequeue();
            }
        };
        proc.Exited += (_, _) =>
        {
            // 同じデータフォルダーで別のバックエンドが動いている（多重起動の防止。2026-10-17 監査 C105）は専用の文に
            var code = SafeExitCode(proc);
            ready.TrySetException(new BackendStartException(code == ExitLocked.ToString() ? "backend_locked" : "backend_exited",
                                                            Current() ? StderrTail() : null));
            if (!Current() || _stopping) return;     // 再起動で差し替えた古いプロセス・止めている最中
            if (!ready.Task.IsCompletedSuccessfully)
            {
                // 起動の途中（READY の前）の終了は StartAsync の例外として MainViewModel が帯とダイアログに出す。ここで Exited を
                // 通知すると、専用の文（backend_locked など）が一般的な「停止しました」に上書きされうる（監査レビュー FG08-R2）
                AppLog.Info(L.F("Log_Backend_ExitedDuringStart", code));
                return;
            }
            _ = ReportExitedAsync(proc, code, gen);
        };
        // Job Object は起動の前に作っておき、起動から Job に入れるまでの隙間を縮める（2026-10-17 監査 C107）
        try { _job ??= new JobObject(); }
        catch (Exception ex) { AppLog.Warn(L.F("Log_Backend_JobCreateFailed", ex.Message)); }
        try
        {
            if (!proc.Start()) throw new BackendStartException("backend_start_failed", null);
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        {
            // Python ではない・壊れた python.exe、ウイルス対策ソフトなどによる実行の阻止。以前は「予期しないエラー」と
            // 出て、Python の指定を直す案内が出なかった（2026-10-20 レビュー R17B-02、docs/backlog.md B-014）
            proc.Dispose();
            throw new BackendStartException("python_start_failed", AppLog.Redact(py + "\n" + ex.Message));
        }
        var old = _proc;
        _proc = proc;
        old?.Dispose();             // 差し替えた古いプロセス（終了済み、または Kill 済み）
        try { _job?.Assign(proc); }
        catch (Exception ex) { AppLog.Warn(L.F("Log_Backend_JobAssignFailed", ex.Message)); }
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        var timeout = Task.Delay(TimeSpan.FromSeconds(45), ct);
        var done = await Task.WhenAny(ready.Task, timeout);
        if (done != ready.Task)
        {
            Kill();
            throw new BackendStartException("backend_timeout", StderrTail());
        }
        Port = await ready.Task;
        AppLog.Info(L.F("Log_Backend_Started", proc.Id, Port));
    }

    private static string SafeExitCode(Process p)
    {
        try { return p.ExitCode.ToString(); } catch (Exception) { return "?"; }
    }

    /// <summary>
    /// 起動した後の予期しない終了を、標準エラーの末尾（原因のトレースバック）とともにログに書いてから通知する。
    /// Exited の時点では標準エラーの読み取りが終わっていないことがあるので、終わるのを少しだけ待つ（孫プロセスが
    /// パイプを持っていると終わらないので、上限つき）。以前は末尾をどこにも残さなかった（2026-10-20 レビュー R17A-01）。
    /// </summary>
    private async Task ReportExitedAsync(Process proc, string code, int gen)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            await proc.WaitForExitAsync(cts.Token);
        }
        catch (Exception) { }
        if (_stopping || gen != Volatile.Read(ref _generation)) return;      // 待つ間に終了・再起動の操作が始まった
        var tail = StderrTail();
        AppLog.Error(tail.Length > 0 ? L.F("Log_Backend_ExitedWithStderr", code, tail) : L.F("Log_Backend_Exited", code));
        Exited?.Invoke(tail);
    }

    /// <summary>
    /// バックエンドを止める: POST /shutdown を送り、終了を待ってから（時間切れなら）木ごと強制終了する。
    /// 応答で実行中の取り消せないジョブ（導入など）が返ったときは長めに待つ。force は利用者が「それでも閉じる」を選んだとき。
    /// </summary>
    public async Task StopAsync(BackendClient? client, bool force = false)
    {
        _stopping = true;
        var p = _proc;
        if (p == null || p.HasExited) return;
        // 長く待つのは、終了の要求が受け付けられた（バックエンドが自分で終わる）ときだけ。要求を送れなかったときは
        // 自分では終わらないので、少しだけ待って強制終了する
        var wait = TimeSpan.Zero;
        try
        {
            if (client != null)
            {
                wait = ExitWaitForced;
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                var r = await client.PostAsync("shutdown", null, cts.Token);
                var waiting = r.Arr("waiting_jobs").Count;
                wait = force ? ExitWaitForced : waiting > 0 ? ExitWaitUncancellable : ExitWait;
                if (waiting > 0 && !force)
                    AppLog.Info(L.F("Log_Backend_WaitUncancellable", waiting, wait.TotalSeconds));
            }
        }
        catch (Exception e) { AppLog.Warn(L.F("Log_Backend_ShutdownRequestFailed", e.Message)); }
        if (wait > TimeSpan.Zero)
        {
            try
            {
                using var cts = new CancellationTokenSource(wait);
                await p.WaitForExitAsync(cts.Token);
            }
            catch (OperationCanceledException) { AppLog.Warn(L.F("Log_Backend_ForceKill", wait.TotalSeconds)); }
        }
        Kill();
    }

    public void Kill()
    {
        _stopping = true;
        try
        {
            if (_proc is { HasExited: false }) _proc.Kill(entireProcessTree: true);
        }
        catch (Exception) { }
    }

    public void Dispose()
    {
        Kill();
        _job?.Dispose();
        _job = null;
        _proc?.Dispose();
    }
}
