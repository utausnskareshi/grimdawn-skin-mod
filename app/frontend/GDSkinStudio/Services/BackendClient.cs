using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GDSkinStudio.Services;

/// <summary>バックエンドのエラー（code は docs/app_architecture.md 8.1 節）。</summary>
public sealed class ApiException(string code, string message, string? detail = null, JsonObject? parameters = null, int status = 0)
    : Exception(message)
{
    public string Code { get; } = code;
    public string? Detail { get; } = detail;
    public JsonObject Params { get; } = parameters ?? new JsonObject();
    public int Status { get; } = status;

    public string? Param(string key) => Params.TryGetPropertyValue(key, out var v) && v != null
        ? (v is JsonValue jv && jv.TryGetValue<string>(out var s) ? s : v.ToJsonString()) : null;

    public static ApiException FromJob(JsonNode? err)
    {
        if (err is not JsonObject o) return new ApiException("INTERNAL", "unknown error");
        return new ApiException((string?)o["code"] ?? "INTERNAL", (string?)o["message"] ?? "",
            (string?)o["detail"], o["params"] as JsonObject);
    }
}

public sealed record JobProgress(double Progress, string? Stage, JsonObject? Detail, string? Message);

/// <summary>バックエンドの HTTP API（127.0.0.1、合言葉つき）。</summary>
public sealed class BackendClient : IDisposable
{
    private readonly HttpClient _http;
    public int Port { get; }

    public BackendClient(int port, string token)
    {
        Port = port;
        _http = new HttpClient(new SocketsHttpHandler { UseProxy = false, PooledConnectionLifetime = TimeSpan.FromMinutes(5) })
        {
            BaseAddress = new Uri($"http://127.0.0.1:{port}/api/v1/"),
            Timeout = TimeSpan.FromSeconds(120),
        };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
    }

    private async Task<JsonNode?> SendAsync(HttpMethod method, string path, JsonNode? body, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(method, path);
        if (body != null) req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        HttpResponseMessage resp;
        try
        {
            resp = await _http.SendAsync(req, ct);
        }
        catch (HttpRequestException e)
        {
            throw new ApiException("BACKEND_UNAVAILABLE", e.Message);
        }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested)
        {
            // 時間切れ（120 秒）ではバックエンドは動いたままで、処理が続いていることがある。「接続できません」「バックエンドを
            // 再起動」（帯は出ず押せない）ではなく、時間切れの文にする（reason timeout。コードは BACKEND_UNAVAILABLE のまま。
            // 2026-10-27 R17A-02）
            throw new ApiException("BACKEND_UNAVAILABLE", "timeout", parameters: new JsonObject { ["reason"] = "timeout" });
        }
        using (resp)
        {
            var text = await resp.Content.ReadAsStringAsync(ct);
            JsonNode? j;
            try { j = JsonNode.Parse(text); }
            catch (JsonException) { throw new ApiException("INTERNAL", $"HTTP {(int)resp.StatusCode}: {text}", status: (int)resp.StatusCode); }
            if (j?["ok"]?.GetValue<bool>() == true) return j["data"];
            var err = j?["error"] as JsonObject;
            throw new ApiException((string?)err?["code"] ?? "INTERNAL", (string?)err?["message"] ?? text,
                (string?)err?["detail"], err?["params"] as JsonObject, (int)resp.StatusCode);
        }
    }

    public Task<JsonNode?> GetAsync(string path, CancellationToken ct = default) => SendAsync(HttpMethod.Get, path, null, ct);
    public Task<JsonNode?> PostAsync(string path, JsonNode? body, CancellationToken ct = default) => SendAsync(HttpMethod.Post, path, body ?? new JsonObject(), ct);
    public Task<JsonNode?> PutAsync(string path, JsonNode body, CancellationToken ct = default) => SendAsync(HttpMethod.Put, path, body, ct);
    public Task<JsonNode?> PatchAsync(string path, JsonNode body, CancellationToken ct = default) => SendAsync(HttpMethod.Patch, path, body, ct);
    public Task<JsonNode?> DeleteAsync(string path, CancellationToken ct = default) => SendAsync(HttpMethod.Delete, path, null, ct);

    /// <summary>ジョブを開始して終わるまで進捗を伝える。成功なら result、失敗なら ApiException、取消なら OperationCanceledException。</summary>
    public async Task<JsonNode?> RunJobAsync(string type, JsonObject parameters, Action<string>? onStarted,
        IProgress<JobProgress>? progress, Action<JsonObject>? onEvent = null, CancellationToken ct = default)
    {
        var job = await PostAsync("jobs", new JsonObject { ["type"] = type, ["params"] = parameters }, ct);
        var id = (string)job!["id"]!;
        onStarted?.Invoke(id);
        long after = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var r = await GetAsync($"jobs/{id}/events?after={after}&wait=10", ct);
            foreach (var ev in r!["events"]!.AsArray())
            {
                if (ev is not JsonObject e) continue;
                after = Math.Max(after, (long)e["seq"]!);
                onEvent?.Invoke(e);
                var kind = (string?)e["kind"];
                if (kind == "progress")
                    progress?.Report(new JobProgress((double?)e["progress"] ?? 0, (string?)e["stage"], e["detail"] as JsonObject,
                        (string?)e["message"]));
                else if (kind == "log")
                    AppLog.FromBackend(((string?)e["level"]) == "warning" ? "WARN" : "INFO", (string?)e["message"] ?? "");
            }
            var state = (string?)r["state"];
            if (state is "succeeded" or "failed" or "cancelled" && r["events"]!.AsArray().Count == 0)
            {
                var detail = await GetAsync($"jobs/{id}", ct);
                if (state == "succeeded") return detail!["result"];
                if (state == "cancelled") throw new OperationCanceledException();
                throw ApiException.FromJob(detail!["error"]);
            }
        }
    }

    public Task<JsonNode?> CancelJobAsync(string jobId) => PostAsync($"jobs/{jobId}/cancel", null);

    public void Dispose() => _http.Dispose();
}
