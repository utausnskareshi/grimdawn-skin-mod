using System.Globalization;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media;
using GDSkinStudio.Localization;

namespace GDSkinStudio.Views;

public sealed class BoolToVisibilityConverter : IValueConverter
{
    public bool Invert { get; set; }

    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
    {
        bool b = value is bool v && v;
        if (Invert) b = !b;
        return b ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        (value is Visibility.Visible) ^ Invert;
}

/// <summary>値が ConverterParameter と同じなら表示する（ページの切り替え）。</summary>
public sealed class KeyToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        Equals(value as string, parameter as string) ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => throw new NotSupportedException();
}

/// <summary>
/// 一覧の項目の頭に記号を付ける（日本語「・項目」、英語「• item」。Strings.resx の Common_BulletFormat）。
/// XAML の StringFormat に書式の穴のある文言は使わない（{l:Tr} は穴の無い文言だけ。tests/test_strings_en.py）ため、変換で付ける。
/// </summary>
public sealed class BulletConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) => L.F("Common_BulletFormat", value);

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => throw new NotSupportedException();
}

public sealed class InverseBoolConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) => !(value is bool b && b);
    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => !(value is bool b && b);
}

public sealed class NullToVisibilityConverter : IValueConverter
{
    public bool Invert { get; set; }

    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        (value == null || (value is string s && s.Length == 0)) ^ Invert ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => throw new NotSupportedException();
}

/// <summary>ok / warning / error / missing → 色。</summary>
public sealed class StatusBrushConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) => (value as string) switch
    {
        "ok" => Application.Current.Resources["OkBrush"],
        "warning" => Application.Current.Resources["WarnBrush"],
        _ => Application.Current.Resources["ErrBrush"],
    };

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => throw new NotSupportedException();
}

/// <summary>ok / warning / error / missing → 記号（Segoe Fluent Icons）。</summary>
public sealed class StatusGlyphConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) => (value as string) switch
    {
        "ok" => "",        // チェック
        "warning" => "",   // 警告
        _ => "",           // ×
    };

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) => throw new NotSupportedException();
}
