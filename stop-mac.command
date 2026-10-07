#!/bin/sh
# Двойной щелчок в Finder останавливает Allur twin 2.0. База и состояние сохраняются.
cd "$(dirname "$0")"
sh ./stop.sh
echo 'Окно можно закрыть.'
