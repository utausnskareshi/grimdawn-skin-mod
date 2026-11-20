using System.IO;
using System.Windows;
using System.Windows.Media.Imaging;

namespace GDSkinStudio.Services;

/// <summary>
/// 画像ファイルの読み込み（入力画像・変換の確認画像・候補の正面図・装備一覧の縮小画像で共通）。
/// new Uri(path) は %41 や %E3%81%82 のような並びを復号して別の名前を開くので、ファイルのストリームから読む。
/// OnLoad で読み終えてからストリームを閉じるので、ファイルをつかんだままにしない。
/// </summary>
public static class ImageFiles
{
    /// <summary>
    /// 読めないときは null。decodeWidth・decodeHeight は縮めて読むとき（0 なら元の大きさ。片方だけなら縦横比を保つ）、
    /// sourceRect は元の画像から切り出す範囲（画素。画像の外にはみ出すと読めずに null）。
    /// </summary>
    public static BitmapImage? Load(string path, int decodeWidth = 0, int decodeHeight = 0, Int32Rect? sourceRect = null)
    {
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var bi = new BitmapImage();
            bi.BeginInit();
            bi.CacheOption = BitmapCacheOption.OnLoad;
            // ストリームから読むときは WPF の画像のキャッシュ（Uri ごと）を通らないので、変換し直しで同じパスが新しい画像に
            // なっても毎回読み直す（IgnoreImageCache はストリームと一緒に指定すると ArgumentNullException になる）
            if (sourceRect is Int32Rect r) bi.SourceRect = r;
            if (decodeWidth > 0) bi.DecodePixelWidth = decodeWidth;
            if (decodeHeight > 0) bi.DecodePixelHeight = decodeHeight;
            bi.StreamSource = fs;
            bi.EndInit();
            bi.Freeze();
            return bi;
        }
        catch (Exception) { return null; }
    }
}
