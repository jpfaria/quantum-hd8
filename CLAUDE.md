# quantum-hd8

## Every change is committed AND pushed
Any change to this repo ends with `git commit` + `git push` to `origin/main` in the same turn —
never leave work only in the working tree or only in a local commit.
**Why:** João installs the lib and the CLI from GitHub (`pip install git+…`); an unpushed fix does not exist for him or for his other sessions.
**How to apply:** run the tests (`python3 -m pytest -q`, check the exit code, not a piped `tail`),
commit, push, and say the commit hash.

## Segurança

- Escrita **nova** (parâmetro ainda não verificado): um por vez, com releitura.
- Todo `set` guarda o valor anterior; `undo` restaura.
- Antes de escrever em saída que alimenta SYN-5050/gabinetes: pedir para baixar
  o volume.
- Nada de `launchctl`, parar ou reiniciar o `ucdaemon` sem pedir antes.
- Subagente de verificação nunca executa contra a interface real.
