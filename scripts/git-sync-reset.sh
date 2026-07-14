#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-$(git branch --show-current)}"

if [[ -z "$BRANCH" ]]; then
  echo "Nao foi possivel descobrir a branch atual."
  exit 1
fi

COMMIT_MESSAGE="${1:-Atualizacao automatica antes do reset - $(date '+%Y-%m-%d %H:%M:%S')}"
REMOTE_REF="$REMOTE/$BRANCH"

echo "Repositorio: $(pwd)"
echo "Branch: $BRANCH"
echo "Remoto: $REMOTE"

git update-index -q --refresh

if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo "Alteracoes encontradas. Criando commit local..."
  git add -A

  if ! git diff --cached --quiet; then
    git commit -m "$COMMIT_MESSAGE"
  else
    echo "Nada para commitar depois do git add."
  fi
else
  echo "Nenhuma alteracao local para commitar."
fi

echo "Baixando novidades do remoto..."
git fetch "$REMOTE" "$BRANCH"

if ! git merge-base --is-ancestor "$REMOTE_REF" HEAD; then
  echo "O remoto tem commits novos. Reaplicando os commits locais por cima..."
  git rebase "$REMOTE_REF"
fi

echo "Enviando branch para o remoto..."
git push "$REMOTE" "HEAD:$BRANCH"

echo "Atualizando copia local para ficar igual ao remoto..."
git fetch "$REMOTE" "$BRANCH"
git reset --hard "$REMOTE_REF"

echo "Pronto. Aplicacao sincronizada com $REMOTE_REF."
