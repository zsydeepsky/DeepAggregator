param(
    [string]$Version = "",
    [string]$Name = "deepaggregator",
    [string]$Namespace = "zsydeepsky",
    [switch]$Push
)

$ErrorActionPreference = "Stop"

if (-not $Version) {
    $Version = Get-Date -Format "yyyyMMdd-HHmm"
}

$root = $PSScriptRoot
$Image = if ($Namespace) { "${Namespace}/${Name}" } else { $Name }
Push-Location $root

try {
    $modelDir = Join-Path $root "model-cache"
    $marker = Join-Path $modelDir "onnx\model_quantized.onnx"
    if (-not (Test-Path $marker)) {
        Write-Host "==> downloading embedding model to model-cache/"
        $py = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { $py = "python" }
        $env:HF_ENDPOINT = "https://hf-mirror.com"
        $code = "from huggingface_hub import snapshot_download; snapshot_download('n24q02m/Qwen3-Embedding-0.6B-ONNX', local_dir='model-cache', allow_patterns=['config.json', 'tokenizer.json', 'tokenizer_config.json', 'onnx/model_quantized.onnx'])"
        $ok = $false
        foreach ($i in 1..3) {
            & $py -c $code
            if ($LASTEXITCODE -eq 0 -and (Test-Path $marker)) { $ok = $true; break }
            Write-Host "==> download attempt $i failed, retrying in 5s"
            Start-Sleep -Seconds 5
        }
        if (-not $ok) { throw "model download failed" }
    }

    Write-Host "==> docker build ${Image}:$Version"
    docker build -t "${Image}:$Version" -t "${Image}:latest" .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

    $outDir = Join-Path $root "docker_image"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    $tarFile = Join-Path $outDir "${Name}-$Version.tar"

    Write-Host "==> docker save -> $tarFile"
    docker save -o $tarFile "${Image}:$Version" "${Image}:latest"
    if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

    $gzFile = "$tarFile.gz"
    Write-Host "==> gzip -> $gzFile"
    $inStream = [System.IO.File]::OpenRead($tarFile)
    $outStream = [System.IO.File]::Create($gzFile)
    $gzip = New-Object System.IO.Compression.GzipStream($outStream, [System.IO.Compression.CompressionLevel]::Fastest)
    $inStream.CopyTo($gzip)
    $gzip.Dispose()
    $inStream.Dispose()
    $outStream.Dispose()
    Remove-Item $tarFile

    $sizeMb = [math]::Round((Get-Item $gzFile).Length / 1MB, 1)
    Write-Host "==> done: $gzFile ($sizeMb MB)"

    if ($Push) {
        Write-Host "==> docker push ${Image}:$Version"
        docker push "${Image}:$Version"
        if ($LASTEXITCODE -ne 0) { throw "docker push failed" }
        Write-Host "==> docker push ${Image}:latest"
        docker push "${Image}:latest"
        if ($LASTEXITCODE -ne 0) { throw "docker push failed" }
        Write-Host "==> pushed: ${Image}:$Version / ${Image}:latest"
    }

    Write-Host "==> deploy options:"
    Write-Host "    [hub]     docker compose up -d          (auto-pulls ${Image}:latest)"
    Write-Host "    [offline] docker load -i $(Split-Path -Leaf $gzFile)"
} finally {
    Pop-Location
}
