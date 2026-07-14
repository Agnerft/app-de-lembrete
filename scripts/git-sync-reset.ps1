$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$remote = if ($env:REMOTE) { $env:REMOTE } else { "origin" }
$branch = if ($env:BRANCH) { $env:BRANCH } else { git branch --show-current }

if (-not $branch) {
    Write-Error "Nao foi possivel descobrir a branch atual."
    exit 1
}

$message = if ($args.Count -gt 0) {
    $args -join " "
} else {
    "Atualizacao automatica antes do reset - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}

$remoteRef = "$remote/$branch"

Write-Host "Repositorio: $repoRoot"
Write-Host "Branch: $branch"
Write-Host "Remoto: $remote"

git update-index -q --refresh

$status = git status --porcelain
if ($status) {
    Write-Host "Alteracoes encontradas. Criando commit local..."
    git add -A

    $staged = git diff --cached --name-only
    if ($staged) {
        git commit -m $message
    } else {
        Write-Host "Nada para commitar depois do git add."
    }
} else {
    Write-Host "Nenhuma alteracao local para commitar."
}

Write-Host "Baixando novidades do remoto..."
git fetch $remote $branch

git merge-base --is-ancestor $remoteRef HEAD
if ($LASTEXITCODE -ne 0) {
    Write-Host "O remoto tem commits novos. Reaplicando os commits locais por cima..."
    git rebase $remoteRef
}

Write-Host "Enviando branch para o remoto..."
git push $remote "HEAD:$branch"

Write-Host "Atualizando copia local para ficar igual ao remoto..."
git fetch $remote $branch
git reset --hard $remoteRef

Write-Host "Pronto. Aplicacao sincronizada com $remoteRef."
