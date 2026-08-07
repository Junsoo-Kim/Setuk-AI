param(
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$versionPath = Join-Path $projectRoot 'VERSION'
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content -Raw -Encoding UTF8 -LiteralPath $versionPath).Trim()
}
if ($Version -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
    throw "올바르지 않은 버전입니다: $Version"
}

$packageName = "Setuk-Harness-$Version"
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'dist'))
$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $distRoot ".staging-$packageName"))
$packageRoot = Join-Path $stagingRoot $packageName
$archivePath = [System.IO.Path]::GetFullPath((Join-Path $distRoot "$packageName.zip"))

function Assert-InDist([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $distRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "배포 폴더 밖의 경로를 변경할 수 없습니다: $fullPath"
    }
}

Assert-InDist $stagingRoot
Assert-InDist $archivePath

$releaseDirectories = @(
    '.agent',
    '.clinerules',
    'python_portable'
)
$releaseFiles = @(
    'AGENTS.md',
    '학생정보/template.yaml',
    '학생정보/example.yaml',
    '학생정보/README.md',
    '보고서/README.md',
    '세특/README.md',
    'linter.py',
    'scripts/extract_docx.py',
    'rules.json',
    'README.md',
    '사용안내.md',
    'LICENSE',
    'THIRD_PARTY_NOTICES.md',
    'VERSION'
)
foreach ($relativePath in ($releaseDirectories + $releaseFiles)) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath))) {
        throw "필수 배포 항목이 없습니다: $relativePath"
    }
}

$python = Join-Path $projectRoot 'python_portable\python.exe'
Write-Host '전체 자동 테스트를 실행합니다.'
& $python '-m' 'unittest' 'discover' '-s' (Join-Path $projectRoot 'tests') '-v'
if ($LASTEXITCODE -ne 0) {
    throw "자동 테스트 실패: 종료 코드 $LASTEXITCODE"
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

foreach ($relativePath in $releaseDirectories) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $relativePath) -Destination $packageRoot -Recurse
}
foreach ($relativePath in $releaseFiles) {
    $destination = Join-Path $packageRoot $relativePath
    $destinationParent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot $relativePath) -Destination $destination
}

$smokeFile = Join-Path $packageRoot '세특\.release-smoke.md'
$smokeText = '수업에서 수집한 자료를 비교하고 결과를 근거로 결론을 수정함.'
[System.IO.File]::WriteAllText($smokeFile, $smokeText, [System.Text.UTF8Encoding]::new($false))
$releasePython = Join-Path $packageRoot 'python_portable\python.exe'
$releaseLinter = Join-Path $packageRoot 'linter.py'
$smokeOutput = & $releasePython $releaseLinter $smokeFile '--json'
$smokeExit = $LASTEXITCODE
if ($smokeExit -ne 0) {
    throw "배포본 Linter 연기 검사 실패: 종료 코드 $smokeExit`n$($smokeOutput -join [Environment]::NewLine)"
}
$smokeResult = ($smokeOutput -join [Environment]::NewLine) | ConvertFrom-Json
if (-not $smokeResult.passed -or $smokeResult.errors -ne 0) {
    throw '배포본 Linter 연기 검사 결과가 올바르지 않습니다.'
}
Remove-Item -LiteralPath $smokeFile -Force

$zipBuilder = Join-Path $projectRoot 'scripts\create_release_zip.py'
& $python $zipBuilder $stagingRoot $archivePath
if ($LASTEXITCODE -ne 0) {
    throw "ZIP 생성 실패: 종료 코드 $LASTEXITCODE"
}
Remove-Item -LiteralPath $stagingRoot -Recurse -Force

$archive = Get-Item -LiteralPath $archivePath
Write-Host "배포 파일 생성 완료: $($archive.FullName)"
Write-Host "크기: $($archive.Length) bytes"
