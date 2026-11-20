using System.Text.Json;
using System.Text.Json.Nodes;

namespace GDSkinStudio.Services;

/// <summary>JsonNode を読むときの小さな補助。</summary>
public static class Json
{
    public static string? Str(this JsonNode? n, string key)
    {
        var v = n?[key];
        if (v is null) return null;
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue<string>(out var s)) return s;
            return jv.ToJsonString();
        }
        return v.ToJsonString();
    }

    public static bool Bool(this JsonNode? n, string key, bool def = false)
    {
        var v = n?[key];
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue<bool>(out var b)) return b;
            if (jv.TryGetValue<int>(out var i)) return i != 0;
        }
        return def;
    }

    public static long? Long(this JsonNode? n, string key)
    {
        var v = n?[key];
        if (v is JsonValue jv)
        {
            if (jv.TryGetValue<long>(out var l)) return l;
            if (jv.TryGetValue<double>(out var d)) return (long)d;
        }
        return null;
    }

    public static double? Dbl(this JsonNode? n, string key)
    {
        var v = n?[key];
        if (v is JsonValue jv && jv.TryGetValue<double>(out var d)) return d;
        return null;
    }

    public static JsonObject? Obj(this JsonNode? n, string key) => n?[key] as JsonObject;

    public static JsonArray Arr(this JsonNode? n, string key) => n?[key] as JsonArray ?? new JsonArray();

    public static IEnumerable<string> Strings(this JsonNode? n, string key) =>
        n.Arr(key).Select(x => x is JsonValue v && v.TryGetValue<string>(out var s) ? s : x?.ToJsonString() ?? "");

    public static JsonNode? Clone(this JsonNode? n) => n == null ? null : JsonNode.Parse(n.ToJsonString());

    public static string Pretty(this JsonNode? n) =>
        n?.ToJsonString(new JsonSerializerOptions { WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping }) ?? "";
}
