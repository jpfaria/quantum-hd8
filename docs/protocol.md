# Protocolo — Universal Control ↔ ucdaemon (UCNet)

Medido em 18/09 contra o `ucdaemon` do Universal Control instalado nesta máquina,
com a Quantum HD 8 ligada. **Medido** = visto em bytes reais (fixtures em
`tests/fixtures/`). **Hipótese** = vem do formato público do UCNet da StudioLive,
ainda não visto aqui.

## Estado (18/09)

| Parte | Status |
|---|---|
| Transporte, enquadramento, handshake, sessão da HD 8 | medido |
| Leitura (`Synchronize`), eventos `PV`/`PL` | medido |
| Escrita `PV` + eco (`global/ledBrightness`), no-op sem eco | medido |
| Cenas: `Listscene` | medido; `RestorePreset` visto no tráfego do UC, não disparado por nós |
| Salvar cena | medido (19/09, captura do UC salvando); `Client.save_scene`/`scene save` disparam, não verificado ao vivo pela ferramenta |
| Medidores `MS levl` (UDP) | layout medido; escala → dBFS calibrada (18/09) |
| Re-amp (saídas 11/12) | seletor do painel fora da camada A; só USB 11/12 confirmado alimentando o re-amp; aux `Out 3/4` e `ADAT 3/4` **não** alcançam (re-medido 19/09, claim anterior era artefato — ver [`camada-b-reamp.md`](camada-b-reamp.md)) |
| `scene load` muda `global/mixerMode` | medido (18/09, MK300-FRFR): `--keep-mode` restaura |
| Curva `fader` (dB ↔ 0..1) | medido (19/09, `line/ch30/aux13`); `quantum_hd8/fader.py`, aplicado a todo parâmetro `curve="fader"` por hipótese (mesma curva na XML) |

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

- `cbytes` = endereço da sessão (ver abaixo); o daemon responde com os dois
  pares trocados. O primeiro probe usou `68 00 65 00` (resposta `65 00 68 00`)
  e só alcançou a sessão raiz; o cliente usa a sessão da HD 8, `6a 00 69 00`.
- `JM`: payload = `uint32 LE len` + JSON.
- `ZM`: payload = `uint32 LE len` (tamanho descomprimido) + zlib. O conteúdo é JSON.
- `KA`: keepalive, payload vazio (mandamos a cada ~1 s; a conexão não caiu em 5 s).

## Handshake do primeiro probe (medido; superado)

> Superado pelas seções "Sessão da HD 8" e "Medidores": o payload do `UM` é
> **só** `uint16 LE` porta UDP, e o estado vem na sessão `6a 00 69 00`.

O primeiro probe mandou `UM` (payload `00 00` + `uint16 LE 47809`) e depois
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
UM   cbytes 00 00 69 00   payload uint16 LE porta UDP (só 2 bytes; ver "Medidores")
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

> **Armadilha (medido, 18/09):** o `FD` chega em outro segmento TCP, até ~4 s
> depois do `ZM`. Quem para de ler no primeiro `ZM` fica com a lista de cenas
> vazia. O cliente lê até ter `ZM`/`ZB` **e** `FD`, sob um único prazo. Teste
> com fake socket precisa entregar os bytes fatiados (em ≥ 2 `recv()`, ou byte a
> byte): entregar a fixture inteira num `recv()` esconde esse bug.

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
- **Carregar cena muda `global/mixerMode` (medido, 18/09):** carregar
  `MK300-FRFR` mudou `global/mixerMode` de `0` para `0,5` -- isso muda o
  roteamento (rótulos do `MixerModeList` do `quantumusbdefs.xml`, índice
  `i` → `i/(n-1)`): `0` = "Mixer Bypass", `0,5` = "Analog + ADAT", `1` =
  "Analog". `quantum-hd8 scene load` sempre compara `global/*` antes/depois
  e avisa em qualquer mudança; `--keep-mode` regrava o valor anterior.
- Sair: `JM {"id":"Unsubscribe"}` em cada sessão.

Strings de ids de mensagem no binário `ucdaemon` (candidatas, não medidas):
`UpdateDevices`, `AudioDeviceInfoRequest`/`AudioDeviceInfoReply`,
`DevicePropertyChanged`, `DeviceUnresponsive`, `ObjectList`, `StringList`.

Fixtures extraídas da captura do UC: `uc-restore.bin` (RestorePreset que o UC
mandou), `uc-recalled.bin` (resposta RecalledPreset), `uc-pvwrite.bin` (PV que o
UC escreveu: `global/mixerMode`).

## Salvar cena (medido, 19/09)

Captura do app UC real salvando a cena `PEDAIS-SYN2-FRFR`:

- UC → daemon, sessão da HD 8 (`JM` cbytes `6a 00 69 00`):
  `{"id": "StorePreset","url": "presets","presetTarget": "","presetFile":
  "scene/PEDAIS-SYN2-FRFR.scene"}` (`tests/fixtures/uc-store.bin` --
  espaçamento/ordem de chaves iguais ao `RestorePreset` de `uc-restore.bin`,
  exceto que `StorePreset` não carrega `presetTargetSlave`).
- daemon → UC (`JM` cbytes `69 00 6a 00`, o par trocado):
  `{"id": "StoredPreset","presetFile": "scene/PEDAIS-SYN2-FRFR.scene",
  "presetName": "scene/PEDAIS-SYN2-FRFR.scene","presetType": "scene"}`
  (`tests/fixtures/uc-stored.bin`).
- Depois de salvar, o UC repetiu o pedido de lista de cenas: `FR` payload
  `02 00 "Listscene" 00 00` -- o primeiro par de bytes parece um contador de
  pedido (`01 00` na conexão, `02 00` depois de salvar); o daemon respondeu
  `FD` com o mesmo contador no cabeçalho.

`Client.save_scene(name)` reproduz essa troca: manda `StorePreset` (byte a
byte igual a `uc-store.bin` para `"PEDAIS-SYN2-FRFR"`), espera até 3 s por
`StoredPreset` (`SceneSaveTimeout` se não chegar) e então manda `FR
Listscene` com o próximo contador (`self._fr_counter`, incrementado a cada
chamada) para atualizar `self.scenes`, esperando o `FD` de resposta.
**Não verificado ao vivo pela ferramenta** -- construído a partir da captura
do UC, não disparado por nós contra o `ucdaemon` real ainda.

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
- Um tom de amplitude 0,5 no USB 4 (vai só para o aux1 → Out 3/4, livres no
  patchbay) não mexeu em nenhum valor de entrada, aux ou main -- medidor de
  canal DAW é pós-fader (o fader do USB 4 estava em −∞) e/ou usa outro modo
  de medição; a escala abaixo só vale para entradas físicas (`in`).

### Calibração (medida, 18/09)

Sinal conhecido numa entrada física: gerador de tom no `USB 11` → **Re-amp 1**
(saída da HD 8, painel em ADAT 1/2, ver [`camada-b-reamp.md`](camada-b-reamp.md))
→ cabo → `In 3` (pré em 19,5 dB). Gravei o pico do CoreAudio de `In 3`

**Lição de método (19/09):** a claim "aux/ADAT 3/4 alcança o re-amp a −40,4 dBFS" caiu por usar o
**máximo** de uma janela de leitura que ainda continha o tom anterior (USB 11), com uma segunda
variável (USB 4) mudando ao mesmo tempo. Re-medindo com a **mediana** dos pacotes do medidor ao
longo de 1,5 s, depois de um tempo de acomodação, e mudando **uma variável por vez**, o resultado
inverteu (não alcança). Regra: medir com mediana pós-acomodação, uma variável por vez; máximo sobre
janela com transição de tom anterior é não confiável.
(`tools/rec`) ao mesmo tempo que lia `meter_raw` de `line/ch3` — 5 níveis de
tom, `tests/fixtures/meter-calibration.json`:

| tom (amplitude) | `meter_raw` | pico CoreAudio | dBFS (`20·log10(pico)`) |
|---|---|---|---|
| 0,003 | 44 | 0,00057 | −64,9 |
| 0,01 | 125 | 0,00191 | −54,4 |
| 0,03 | 375 | 0,00571 | −44,9 |
| 0,1 | 1251 | 0,01908 | −34,4 |
| 0,2 | 2504 | 0,03802 | −28,4 |

`meter_raw / pico ≈ 65536` em todo o intervalo (ponto mais baixo mais ruidoso:
diferença até ~1,5 dB; os outros quatro batem em ≤0,3 dB) -- ou seja `raw` é o
pico linear × 65535: **dBFS = 20·log10(raw / 65535)**, `raw` 0 (silêncio) →
`-inf`. Implementado em `quantum_hd8.ucnet.meter_dbfs()`; é **pico, não RMS**
(o valor mais alto visto na janela do pacote, não uma média). `meters` e
`meters --once` mostram o valor cru e o dBFS calibrado lado a lado.

## Curva `fader` (medida, 19/09)

Método (`tests/fixtures/fader-curve.json`): tom 0,1 (-20 dBFS) no USB 4
(`line/ch30`), `line/ch30/aux13` (Loopback 1) variado em 17 pontos entre 0,05
e 1,0; para cada ponto, mediana do medidor calibrado de `aux/ch13` após
1,2 s de assentamento; ganho = dBFS + 20 (o tom de entrada). 0 (fundo do
fader) não foi medido em dBFS -- é `-inf` por definição (mute).

Pontos medidos (`normalized` → `gain_db`): 0,05 → -49,48; 0,1 → -39,3; 0,15 →
-35,27; 0,2 → -31,32; 0,3 → -23,44; 0,4 → -15,53; 0,5 → -8,87; 0,6 → -5,1;
0,65 → -3,21; 0,7 → -1,32; **0,735 → 0,0 (unidade)**; 0,75 → 0,57; 0,8 → 2,45;
0,85 → 4,34; 0,9 → 6,23; 0,95 → 8,11; 1,0 → 10,0.

`quantum_hd8.fader.fader_db()`/`fader_normalized()` interpolam linearmente
entre esses pontos (e entre eles, a inversa); abaixo de 0,05 extrapolam a
inclinação do primeiro segmento e prendem em -96 dB (o piso do parâmetro).
**Hipótese, não medida por caminho:** a curva foi medida numa única saída
(`line/ch30/aux13`) e é aplicada a todo parâmetro cujo `curve` no
`params.json` é `"fader"` (todo `volume` e `auxN` de canal, `main/ch1/volume`)
-- eles compartilham o mesmo nome de curva na XML e a mesma faixa relatada
pelo daemon (-96..+10 dB).
