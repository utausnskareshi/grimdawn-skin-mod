using System.IO;
using System.Windows.Input;

namespace GDSkinStudio.ViewModels;

public sealed partial class GenerateViewModel
{
    /// <summary>GLB の欄のパスの存在（ネットワーク上のパスは UI スレッドの外で確かめた結果。点検 R18B-04）。</summary>
    private readonly PathExistence _glbExists;
}

/// <summary>
/// 入力のパス（画像・GLB）の存在の確認（点検 R18B-04）。コマンドの確かめ直し（クリック・キー入力・フォーカスの変化のたび）は
/// UI スレッドで呼ぶので、ネットワーク上のパス（UNC・ネットワークドライブ）は欄を変えたときに UI スレッドの外で確かめ、その
/// 結果を使う（届かないサーバーでは File.Exists が数秒〜数十秒戻らず、画面が固まるのを繰り返した）。ローカルのパスは今までどおり
/// その場で確かめる。結果が古くなっても、生成の開始時にバックエンドがパスを確かめる（無ければエラーの表示）。
/// </summary>
public sealed class PathExistence
{
    /// <summary>入力中は、最後の変更からこの間をおいて確かめる（1 文字ごとにサーバーへ問い合わせない）。</summary>
    public static readonly TimeSpan Debounce = TimeSpan.FromMilliseconds(400);
    /// <summary>見つからなかったネットワーク上のパスを確かめ直す間隔（スリープから戻った NAS・つなぎ直した VPN など）。</summary>
    public static readonly TimeSpan RecheckInterval = TimeSpan.FromSeconds(10);

    private readonly Action _changed;
    private readonly Action? _found;
    private string? _path;              // 確かめた（確かめている）ネットワーク上のパス
    private bool _exists, _pending;
    private int _seq;                   // 確かめる番号（途中でパスが変わったら、古い結果を捨てる）
    private DateTime _checkedAt;

    /// <summary>changed: 確かめた結果が出たとき（UI スレッド）。found: 見つからなかったパスを確かめ直して見つかったとき。</summary>
    public PathExistence(Action changed, Action? found = null)
    {
        _changed = changed;
        _found = found;
    }

    /// <summary>ネットワーク上のパスか（UNC の \\server\share・//server/share・\\?\UNC\…、割り当てたネットワークドライブ）。
    /// \\?\C:\ などの書き方も UNC と同じに扱う（確かめるのが UI スレッドの外になるだけ）。</summary>
    public static bool IsNetworkPath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path)) return false;
        if (path.Length >= 2 && (path[0] is '\\' or '/') && (path[1] is '\\' or '/')) return true;
        if (path.Length < 2 || path[1] != ':' || !char.IsAsciiLetter(path[0])) return false;
        try { return new DriveInfo(path[..1]).DriveType == DriveType.Network; }     // ドライブの種類だけ（サーバーには問い合わせない）
        catch (Exception) { return false; }
    }

    /// <summary>path があるか（UI スレッド）。ネットワーク上のパスは確かめた結果（確かめている間は false）。</summary>
    public bool Exists(string path)
    {
        if (!IsNetworkPath(path)) return File.Exists(path);
        if (!string.Equals(path, _path, StringComparison.Ordinal))
        {
            _ = ProbeAsync(path);
            return false;
        }
        if (!_exists && !_pending && DateTime.UtcNow - _checkedAt >= RecheckInterval)
            _ = RunAsync(++_seq, path, TimeSpan.Zero, recheck: true);
        return _exists;
    }

    /// <summary>欄のパスが変わったとき（UI スレッド）。</summary>
    public void Probe(string path) => _ = ProbeAsync(path);

    /// <summary>
    /// パスが変わったときに確かめる（UI スレッド）。ローカルのパスはその場で確かめ、ネットワーク上のパスは少し待ってから UI
    /// スレッドの外で確かめる。途中でパスが変わったときは false。
    /// </summary>
    public Task<bool> ProbeAsync(string path)
    {
        int seq = ++_seq;
        _path = null;
        _exists = false;
        _pending = false;
        if (!IsNetworkPath(path)) return Task.FromResult(File.Exists(path));
        _path = path;
        return RunAsync(seq, path, Debounce, recheck: false);
    }

    private async Task<bool> RunAsync(int seq, string path, TimeSpan delay, bool recheck)
    {
        _pending = true;
        if (delay > TimeSpan.Zero) await Task.Delay(delay);
        if (seq != _seq) return false;
        bool ok;
        try { ok = await Task.Run(() => File.Exists(path)); }
        catch (Exception) { ok = false; }
        if (seq != _seq) return false;
        _pending = false;
        _exists = ok;
        _checkedAt = DateTime.UtcNow;
        _changed();
        CommandManager.InvalidateRequerySuggested();
        if (ok && recheck) _found?.Invoke();
        return ok;
    }
}
