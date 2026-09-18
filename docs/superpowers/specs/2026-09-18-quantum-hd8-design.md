# quantum-hd8 — design (18/09/2026)

Controlar **tudo** da PreSonus Quantum HD 8 sem o Universal Control (UC):
mixer, cenas, pré-amps, fontes de fone/S-PDIF/loopback, medidores — e mapear o
que o UC não mostra, em especial o **roteamento das saídas de re-amp (11/12)**.
Mesmo formato dos repos `hotone-ampero-2` e `mvave`: lib Python + CLI + plugin
do Claude Code (skill) + `docs/protocol.md`. Repo público, MIT.

## Fatos medidos antes do design (18/09)

- O app UC não fala com a interface: fala com o `ucdaemon` (processo root em
  `/Library/Application Support/Presonus/universalcontrol/ucdaemon.bundle`)
  por **TCP `127.0.0.1:59791`**.
- O UC também abre **UDP `*:47809`** (porta de descoberta do UCNet da PreSonus).
- CoreMIDI expõe `Quantum HD 8 MIDI` (DIN) e `Quantum HD 8 Control`.

## Arquitetura — duas camadas

**Camada A — `ucdaemon` (TCP 59791).** O cliente conecta como um segundo UC.
O UCNet tem formato parcialmente público (projetos da StudioLive: header
`UC\x00\x01`, mensagens `JM` = JSON, `PV` = valor de parâmetro, `KA` = keepalive).
Hipótese a medir no passo 1, não fato: o daemon da HD 8 fala o mesmo dialeto.
1. Conectar e se inscrever **só leitura** → baixar a árvore completa de
   parâmetros. Vira o mapa inicial.
2. Capturar o tráfego UC ↔ daemon enquanto um controle muda, para confirmar o
   formato da escrita.

**Camada B — USB (daemon ↔ HD 8).** Análise estática do binário do `ucdaemon`
(strings, tabelas de parâmetro, códigos de comando) atrás de parâmetros que a
árvore da camada A não expõe — o alvo é o roteamento de re-amp. Prova no device
só depois, um parâmetro por vez. Se a camada A já expuser o re-amp, a camada B
fica só como documentação.

## Componentes

| Unidade | Papel |
|---|---|
| `quantum_hd8/ucnet.py` | Codec dos pacotes (encode/decode), sem I/O |
| `quantum_hd8/client.py` | Conexão TCP, subscribe, keepalive, get/set, eventos |
| `quantum_hd8/params.yaml` | Mapa: caminho, tipo, faixa, unidade, `verified`, `hidden` |
| `quantum_hd8/cli.py` | Comando `quantum-hd8` |
| `tools/probe.py` + `listen --raw` | Grava bytes do daemon (inclusive o eco do que o UC muda) para fixture — sem sniffer, sem sudo |
| `tools/diff_state.py` | Diff entre dois `dump` → qual parâmetro mudou |
| `skills/quantum-hd8/SKILL.md` | Skill do plugin (CLI + regras de segurança) |
| `docs/protocol.md` | Protocolo byte a byte |

Cada entrada de `params.yaml` é `verified: true` só quando o efeito foi medido
no áudio; lida da árvore sem medição = `verified: false`. Veio do binário e o
UC não mostra = `hidden: true`.

## CLI

```
quantum-hd8 state | dump | get <param> | set <param> <valor> | undo
quantum-hd8 scene list | load <nome> | save <nome>
quantum-hd8 preamp <ch> gain <dB> | phantom on|off
quantum-hd8 route <bus> <fonte>      # fones, S/PDIF, loopback, re-amp (se existir)
quantum-hd8 meters                    # ao vivo
quantum-hd8 listen                    # eventos do daemon
```

## Segurança

- Escrita **nova** (parâmetro ainda não verificado): um por vez, com releitura.
- Todo `set` guarda o valor anterior; `undo` restaura.
- Antes de escrever em saída que alimenta SYN-5050/gabinetes: pedir para baixar
  o volume.
- Nada de `launchctl`, parar ou reiniciar o `ucdaemon` sem pedir antes.
- Subagente de verificação nunca executa contra a interface real.

## Testes

- **Offline:** codec com fixtures capturadas (`tests/fixtures/*.bin`); roda sem
  a interface.
- **Device:** `pytest -m device`, opt-in; um parâmetro por vez, restaura o
  valor original no teardown.
- **Roteamento:** prova por tom injetado (`music-setup/tools/tone`) +
  gravação no canal esperado (`music-setup/tools/rec`).

## Documentação

- Protocolo: `docs/protocol.md` neste repo.
- Efeito no rig (novo roteamento, re-amp alcançável etc.):
  `music-setup/docs/equipamentos/presonus-quantum-hd8.md`.

## Fora de escopo

- Firmware update, reset de fábrica, qualquer escrita em flash que não seja a
  mesma que o UC já faz (salvar cena).
- Outros modelos Quantum (não testados; o design não os impede).
