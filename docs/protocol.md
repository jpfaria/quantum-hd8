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
