# Protocolo — Universal Control ↔ ucdaemon (UCNet)

Medido em 18/09 contra o `ucdaemon` do Universal Control instalado nesta máquina,
com a Quantum HD 8 ligada. **Medido** = visto em bytes reais (fixtures em
`tests/fixtures/`). **Hipótese** = vem do formato público do UCNet da StudioLive,
ainda não visto aqui.

## Transporte

- TCP `127.0.0.1:59791`. O app UC fica conectado nele (visto com `lsof`).
- Descoberta: o daemon manda UDP para a porta `47809`. Pacote medido:
  `UC\x00\x01` + porta TCP em LE (`8f e9` = 59791) + `DA` + … +
  `Quantum HD 8/771\0AUD\0QT9E25260495\0` (modelo/id, tipo, serial).
- Conectar e só escutar: o daemon não manda nada (medido, 4 s).

## Enquadramento (medido)

```
"UC" 00 01 | size: uint16 LE | code: 2 ASCII | cbytes: 4 | payload
size = 6 + len(payload)   (cobre code + cbytes + payload)
```

- `cbytes` que mandamos: `68 00 65 00`. O daemon responde com `65 00 68 00`
  (os dois pares trocados).
- `JM`: payload = `uint32 LE len` + JSON.
- `ZM`: payload = `uint32 LE len` (tamanho descomprimido) + zlib. O conteúdo é JSON.
- `KA`: keepalive, payload vazio (mandamos a cada ~1 s; a conexão não caiu em 5 s).

## Handshake (medido)

Mandamos `UM` (payload `00 00` + `uint16 LE 47809`) e depois
`JM {"id":"Subscribe", "clientName", "clientInternalName", "clientType",
"clientDescription", "clientIdentifier", "clientOptions", "clientEncoding"}`
(os mesmos campos aparecem como strings no binário do daemon).

Resposta (`tests/fixtures/probe-subscribe-rx.bin`):
1. `ZM` → `{"id": "UpdateMidiEndpoints","endpointList": []}`
2. `JM` → `{"id": "SubscriptionReply"}`

Isso é a sessão **raiz** do daemon: com `cbytes` `68 00 65 00` ela só traz os
endpoints MIDI. A árvore da HD 8 vem em outra sessão (abaixo).

## `cbytes` são o endereço da sessão (medido, 18/09)

Captura do app UC real (`sudo tcpdump -i lo0 tcp port 59791`, feita pelo João)
mostrou várias sessões na mesma conexão TCP, cada uma com seu par de `cbytes`
`[a 00 b 00]`; o daemon responde com o par trocado `[b 00 a 00]`:

| cbytes (cliente→daemon) | O que vem |
|---|---|
| `69 00 65 00` | raiz: `UpdateMidiEndpoints` |
| `64 00 67 00` | `UpdateDevices` (lista: `"name": "Quantum HD 8"`, `firmwareRevision: 771`, serial) + `InvokeMethod getOptions` |
| `6a 00 69 00` | **a sessão da HD 8**: estado, escrita, cenas |

## Sessão da HD 8 (medido e reproduzido sem o UC)

```
UM   cbytes 00 00 69 00   payload 00 00 + uint16 LE porta UDP
JM   cbytes 6a 00 69 00   {"id":"Subscribe","clientName":"quantum-hd8","clientInternalName":"ucapp", …}
FR   cbytes 6a 00 69 00   01 00 "Listscene" 00 00          (lista de cenas)
```

Respostas (`tests/fixtures/probe-device-rx.bin`):
1. `ZM` → `{"id":"Synchronize","children":{"global","line","aux","main"},"shared":…}`.
   Cada nó tem `values` (valor **normalizado 0..1**, ou string), `ranges`
   (`min`, `max`, `def`, `units`, `curve`, `mid`) e `children`. São **os mesmos
   1419 caminhos** do `quantumusbdefs.xml` (0 a mais, 0 a menos) — **nenhum
   parâmetro de re-amp** na camada A.
2. `JM` → `SubscriptionReply`.
3. `FD` → cabeçalho binário (14 bytes) + JSON `{"files":[{"name":"ELEMENT.scene",…}]}`.

Eventos que o daemon empurra para a sessão (medido):
- `PV` → `caminho` + `00` + `uint16 LE` (flag, visto 0) + `float32 LE` normalizado. Ex.: `aux/ch1/volume`
  = `0x3f3c28f6` = 0,735.
- `PL` → `caminho` + `00` + `uint16 LE` (flag; visto 0 e 1) + `float32 LE` valor
  normalizado + rótulos separados por `\n` + `00`. Ex.: `global/mainOutVolumeLink`
  = 0,333 com `None\nAll\n1-2\n1-4\n1-6\n1-8\nAll + ADAT`
  (`tests/fixtures/uc-pl.bin`)
  (fontes de fone/S-PDIF: `Main L/R`, `Out 3/4` … `ADAT 15/16`, `Loopback 1`,
  `Loopback 2`, `S/PDIF Out`).
- `JM RecalledPreset` depois de carregar cena.

## Escrita (medido no tráfego do UC)

- Parâmetro: o UC manda `PV` na sessão `6a 00 69 00` com o mesmo formato
  (visto: `global/mixerMode` = 0,5).
- Cena: `JM {"id":"RestorePreset","url":"presets","presetTarget":"",
  "presetTargetSlave":0,"presetFile":"scene/MK300-FRFR.scene"}` → o daemon
  responde `RecalledPreset` e reenvia os `PV` de tudo.
- Sair: `JM {"id":"Unsubscribe"}` em cada sessão.

Strings de ids de mensagem no binário `ucdaemon` (candidatas, não medidas):
`UpdateDevices`, `AudioDeviceInfoRequest`/`AudioDeviceInfoReply`,
`DevicePropertyChanged`, `DeviceUnresponsive`, `ObjectList`, `StringList`.

Fixtures extraídas da captura do UC: `uc-restore.bin` (RestorePreset que o UC
mandou), `uc-recalled.bin` (resposta RecalledPreset), `uc-pvwrite.bin` (PV que o
UC escreveu: `global/mixerMode`).

## Eco da escrita (medido, 18/09)

Escrevendo `PV global/ledBrightness` na sessão `6a 00 69 00`, o daemon devolve
um `PV` com o valor aplicado em ~ms (`tests/fixtures/led-write-echo-rx.bin`).
Parâmetro inteiro volta quantizado: pedi 0,2 → veio 0,202 (= passo de 1/99 na
faixa 1..100). Restaurei o valor original (0,7475) e a releitura numa conexão
nova bateu.

## Medidores (medido, 18/09)

- **Correção do handshake:** o payload do `UM` é **só** `uint16 LE` porta UDP
  (2 bytes). O UC manda assim (captura: `7d ec`). Com `00 00` na frente o daemon
  lê porta 0 e não manda medidor nenhum.
- Com a porta certa, o daemon manda por **UDP** para `127.0.0.1:<porta>` ~4–5
  pacotes/s, 171 bytes cada (`tests/fixtures/meters-udp-1.bin`):

```
"UC" 00 01 | size | "MS" | cbytes 69 00 6a 00 | "levl" 00 00 | uint16 LE n=66
| n × uint16 LE valores | rodapé
```

- Rodapé (18 bytes + 1 byte `00` de sobra, 19 no total): ao contrário do
  resto do pacote, os campos do rodapé são **big-endian** (medido: `uint16
  LE` dava lixo tipo 9216/1024; `uint16 BE` dá os valores documentados
  abaixo). Lido como 9× `uint16 BE`: `(768, 0, 36, 4, 36, 28, 7, 64, 2)`.
  Os pares `(offset, count)` das seções `in`/`aux`/`main` são os campos 2/3,
  5/6 e 8/9 (1-based): `(0, 36)`, `(36, 28)`, `(64, 2)` -- o campo antes de
  cada par (`4`, `7`, ainda não confirmado para o terceiro) provavelmente é
  um tipo, não provado. `quantum_hd8.ucnet.parse_meters()` usa só os pares
  `(offset, count)`; cai para `{"raw": values}` se não baterem com esse
  layout. Leitura:
  - valores 0–35 → entradas `line/ch1..36`
  - valores 36–63 → 14 saídas `aux` × L/R
  - valores 64–65 → `main` L/R
- Prova parcial: `line/ch16` e `ch17` (ADAT 6/7 = ADA #1 In 6/7, retorno da
  MK-300) marcam ~270–450 com a pedaleira ligada; entradas sem nada marcam 0–1.
- **Não calibrado:** um tom de amplitude 0,5 no USB 4 (vai só para o aux1 →
  Out 3/4, livres no patchbay) não mexeu em nenhum valor de entrada, aux ou
  main. Hipóteses abertas: medidor de canal DAW pós-fader (o fader do USB 4
  está em −∞) e/ou o UC pedir outro modo de medição (`global/meterSource`?).
  A escala (valor → dBFS) precisa de um sinal conhecido numa entrada física.
