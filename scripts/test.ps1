<#
.SYNOPSIS
    Setuk-AI 저장소의 단일 테스트 진입점.

.DESCRIPTION
    코딩 에이전트나 사람이 매번 "어떤 명령으로 테스트할지"를 즉석에서 고르지 않도록,
    저장소 루트 AGENTS.md가 정한 검증 절차를 그대로 실행한다.

    -Scope Changed (기본값): 워킹 트리에서 변경된 파일이 속한 버전(B·C)의 전체 로컬
        테스트만 실행한다. 커밋 직전 게이트에 대응한다.
    -Scope Full: 변경 여부와 무관하게 B·C 전체 테스트를 모두 실행한다. Push/PR 게이트.
    -Scope Release: Full을 실행한 뒤 `version_B/scripts/build_release.ps1`로 실제 배포
        ZIP까지 만들어 배포 연기 검사를 완료한다. 배포 직전 게이트.

    버전별 실행 환경은 고정되어 있다 — B버전은 `version_B/python_portable`, C버전은
    `version_C/.venv`. 이 스크립트는 시스템 Python을 사용하지 않는다.

.PARAMETER Scope
    Changed | Full | Release

.EXAMPLE
    .\scripts\test.ps1 -Scope Changed
.EXAMPLE
    .\scripts\test.ps1 -Scope Full
.EXAMPLE
    .\scripts\test.ps1 -Scope Release
#>

param(
    [ValidateSet('Changed', 'Full', 'Release')]
    [string]$Scope = 'Changed'
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Write-Section([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Test-VersionB {
    Write-Section 'version_B (python_portable)'
    $python = Join-Path $RepoRoot 'version_B\python_portable\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw "version_B/python_portable/python.exe가 없습니다. 배포용 포터블 Python을 먼저 준비하세요."
    }
    Push-Location (Join-Path $RepoRoot 'version_B')
    try {
        & $python '-m' 'unittest' 'discover' '-s' 'tests' '-v'
        if ($LASTEXITCODE -ne 0) {
            throw "version_B 테스트 실패: 종료 코드 $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

function Test-VersionC {
    Write-Section 'version_C (.venv)'
    $python = Join-Path $RepoRoot 'version_C\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw "version_C/.venv/Scripts/python.exe가 없습니다. 가상환경을 먼저 만드세요 (python -m venv version_C/.venv)."
    }
    Push-Location (Join-Path $RepoRoot 'version_C')
    try {
        & $python '-m' 'unittest' 'discover' '-s' 'tests' '-v'
        if ($LASTEXITCODE -ne 0) {
            throw "version_C 테스트 실패: 종료 코드 $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

function Get-ChangedVersions {
    Push-Location $RepoRoot
    try {
        $status = & git status --porcelain 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'git 저장소가 아니거나 git을 사용할 수 없습니다. 안전하게 B·C 모두 실행합니다.' -ForegroundColor Yellow
            return @('version_B', 'version_C')
        }
    } finally {
        Pop-Location
    }

    $touched = New-Object System.Collections.Generic.HashSet[string]
    foreach ($line in $status) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        # `git status --porcelain` 형식: "XY <path>" (rename은 "XY <old> -> <new>").
        $path = $line.Substring(3)
        if ($path -match '->') {
            $path = ($path -split '->')[-1].Trim()
        }
        $path = $path.Trim('"')
        if ($path -match '^(version_B|version_C)/') {
            [void]$touched.Add($Matches[1])
        }
    }

    if ($touched.Count -eq 0) {
        Write-Host 'version_B, version_C 안의 변경 사항이 없습니다.' -ForegroundColor Yellow
        return @()
    }
    return $touched
}

switch ($Scope) {
    'Changed' {
        $versions = Get-ChangedVersions
        if ($versions.Count -eq 0) {
            Write-Host '실행할 테스트가 없습니다 (B·C 변경 없음).'
            return
        }
        if ($versions -contains 'version_B') { Test-VersionB }
        if ($versions -contains 'version_C') { Test-VersionC }
    }
    'Full' {
        Test-VersionB
        Test-VersionC
    }
    'Release' {
        Test-VersionB
        Test-VersionC
        Write-Section '배포 ZIP 생성 + 압축본 연기 검사 (version_B/scripts/build_release.ps1)'
        & (Join-Path $RepoRoot 'version_B\scripts\build_release.ps1')
        if ($LASTEXITCODE -ne 0) {
            throw "배포 빌드 실패: 종료 코드 $LASTEXITCODE"
        }
    }
}

Write-Host ''
Write-Host "[$Scope] 테스트 완료." -ForegroundColor Green
