param(
    [string]$BaseUrl = 'http://127.0.0.1:18888',
    [switch]$TestGemini,
    [switch]$TestGeminiStream
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$client = New-Object System.Net.Http.HttpClient($handler)
$client.Timeout = [TimeSpan]::FromSeconds(30)

try {
    $healthJson = $client.GetStringAsync("$BaseUrl/__bridge/health").GetAwaiter().GetResult()
    $health = $healthJson | ConvertFrom-Json
    if ($health.status -ne 'ok' -or -not $health.native_channel_connected -or $health.executor -ne 'offscreen') {
        throw "Bridge is not ready: $healthJson"
    }
    Write-Host 'Bridge health: OK (native channel + offscreen executor)' -ForegroundColor Green

    if ($TestGemini -or $TestGeminiStream) {
        $apiKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
        if ([string]::IsNullOrWhiteSpace($apiKey)) {
            throw 'GEMINI_API_KEY is not set in this process.'
        }

        if ($TestGemini) {
            $request = New-Object System.Net.Http.HttpRequestMessage(
                [System.Net.Http.HttpMethod]::Get,
                "$BaseUrl/gemini/v1beta/models"
            )
            $request.Headers.Add('x-goog-api-key', $apiKey)
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                throw "Gemini verification failed with HTTP $([int]$response.StatusCode): $body"
            }
            $models = ($body | ConvertFrom-Json).models
            $gemini3 = @($models | Where-Object { $_.name -match '^models/gemini-3' })
            Write-Host "Gemini authentication: OK ($($models.Count) models; $($gemini3.Count) Gemini 3+ entries)" -ForegroundColor Green
            $response.Dispose()
            $request.Dispose()
        }

        if ($TestGeminiStream) {
            $fixturePath = Join-Path $PSScriptRoot 'fixtures\gemini_stream_request.json'
            $request = New-Object System.Net.Http.HttpRequestMessage(
                [System.Net.Http.HttpMethod]::Post,
                "$BaseUrl/gemini/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse"
            )
            $request.Headers.TryAddWithoutValidation('x-goog-api-key', $apiKey) | Out-Null
            $request.Headers.TryAddWithoutValidation('x-app', 'claude-code') | Out-Null
            $request.Headers.TryAddWithoutValidation('x-stainless-runtime', 'node') | Out-Null
            $request.Headers.TryAddWithoutValidation('anthropic-dangerous-direct-browser-access', 'true') | Out-Null
            $request.Content = New-Object System.Net.Http.StringContent(
                (Get-Content -Raw -Encoding utf8 $fixturePath),
                [System.Text.Encoding]::UTF8,
                'application/json'
            )
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                throw "Gemini streaming verification failed with HTTP $([int]$response.StatusCode)."
            }
            if ($body -notmatch 'FANVPN_NATIVE_STREAM_OK') {
                throw 'Gemini streaming verification returned an unexpected response.'
            }
            Write-Host 'Gemini native streaming + browser header filtering: OK' -ForegroundColor Green
            $response.Dispose()
            $request.Dispose()
        }
    }
} finally {
    $client.Dispose()
    $handler.Dispose()
}
