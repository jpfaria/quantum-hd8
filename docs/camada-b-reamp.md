# Camada B: re-amp outs (CoreAudio 11/12) no Quantum HD 8, análise estática

Data: 2026-09-18. Só análise estática (strings, otool, objdump arm64 e Thumb, python). Nada foi
executado contra o daemon nem contra a interface.

Rótulos: **[lido]** = lido no binário (endereço/constante citado). **[hipótese]** = inferência.
Endereços do plugin são VM do slice arm64 de `quantumusbdevice.dylib` (VM = offset no arquivo para
`__TEXT`/`__const`). Endereços `0x30xxxxxx` são do firmware do HD 8 embutido no plugin (ver §0).

Scratch: `/private/tmp/claude-501/.../scratchpad/re-reamp/` (`usb_arm64`, `dis.txt`, `hd8fw.bin`,
`hd8fw.elf`, `fw.dis`).

---

## Resposta curta

1. "Reamp 1/2" e "Reamp Out" **não são** parâmetros do mixer do plugin. Estão dentro do
   **firmware do HD 8**, que vem embutido no plugin: "Reamp 1/2" é nome de canal de playback USB
   (saídas 11/12), e "Reamp Out" é um item do menu **Global Settings do painel frontal**, ligado ao
   parâmetro de firmware `reampOutSource` (id 35). **[lido]**
2. As opções de `reampOutSource` são **"ADAT 1/2", "ADAT 3/4", …, "ADAT 15/16"** (8 opções, 0..7).
   **[lido]**
3. `aux/ch13-14` (`QuantumUsbHD8DawOutputComponent`) são os barramentos **"Loopback 1" e "Loopback 2"**
   do mixer. Não alimentam as saídas re-amp. **[lido]**
4. O mixer do UC tem 14 aux fixos: 4 analógicos (Out 3/4..9/10), 8 ADAT e 2 loopback, mais o Main.
   **Nenhum destino é re-amp.** Nenhum comando do plugin escreve `reampOutSource`. **[lido]**
5. Sobre o protocolo host→device (`PaRi`): o firmware **reporta** `reampOutSource` ao host no
   seletor global **12**, mas o handler de escrita do firmware **ignora o seletor 12** (no-op).
   Então, pelo caminho que o UC usa, o host **não consegue mudar** o re-amp. Só pelo painel frontal.
   **[lido, confiança média-alta]**
6. Caminho indireto possível, a verificar **medindo**: pelo painel, `Reamp Out = ADAT k/k+1`. Se
   "ADAT k/k+1" for o barramento de saída ADAT (que no mixer é `aux/ch(4+k)`), então a mix desse aux
   sai também no re-amp. Mas pode ser o par de **entrada** ADAT (uso standalone). **[hipótese]**

---

## 0. Onde está o firmware

- Em `quantumusbdevice.dylib`, o `__const` (arm64: 0xa0340..0x58ad87) carrega imagens de firmware
  (Thesycon `TL#IMG#METADATA`, RTOS "RTX V5.5.3"): ES 2, ES 4, HD 2 e **HD 8**. **[lido]**
- A imagem do HD 8 começa no offset de arquivo **0x34b5e7** (vetor de reset: SP=0x20000c00,
  Reset=0x300806c1), e a base de carga é **0x30080000** (metadado IMAGE_BASE=0x30080000,
  FLASH_BASE=0x30000000, em 0x3ed04a..). A imagem vai até cerca de 0x58a000. **[lido]**
  Isso foi confirmado por ponteiros: o array `{"Off","Android","Android+"}` está em 0x302b9588
  e aponta para strings da própria imagem.
- A interface do display (menu, medidores `reampOut1/meterdisplay` etc.) faz parte **da mesma
  imagem** (MCU único). **[lido]**

## 1. "Reamp 1/2", "Reamp Out", `reampOutSource`

### 1a. Nomes de canal USB [lido]
Firmware HD 8, strings 0x546f0b..: `Main Left, Main Right, Output 3..10, Reamp 1, Reamp 2,
SPDIF Out L/R, ADAT Out 1..16` (30 saídas, o que bate com o CoreAudio: Out 11/12 = Reamp).
Seguem as entradas `Instrument/Mic/Line 1..2, Mic/Line 3..8, Loopback 1/2 L/R, SPDIF In, ADAT In 1..16`.
São nomes de canal (descritor USB/driver), **não** uma lista de origens.

### 1b. Tabela de parâmetros do firmware [lido]
Registros de 0x54 bytes: `u32 tipo, u32 id, char nome[], float min,max,?,def,step, …, u32 ptr_enum`.
Globais (arquivo 0x543f6b..):

| id | nome | tipo | faixa |
|---|---|---|---|
| 9 | mobileMode | enum | Off/Android/Android+ |
| 31 | headphoneSelect | int | 0..7 |
| 34 | headphoneSource | int | 0..14 |
| **35** | **reampOutSource** | **enum** | **0..7, ptr 0x2036ea88** |
| 36 | spdifSource | int | 0..15 |
| 41 | standaloneProcessingMode | enum | Mixer/ADAT |

A mesma tabela (mesmos ids) existe nas imagens ES2/ES4/HD2. É código comum. **[lido]**

### 1c. Opções do enum [lido]
Array de ponteiros RW em 0x302b95bc (offset de arquivo 0x584ba3), que vira RAM 0x2036ea88:
`ADAT 1/2, ADAT 3/4, ADAT 5/6, ADAT 7/8, ADAT 9/10, ADAT 11/12, ADAT 13/14, ADAT 15/16`.
A string "ADAT 1/2" (0x302783fc) só tem **um** ponteiro na imagem inteira, que é este.
Os outros enums resolvidos pelo mesmo método: clockSource = Internal/S/PDIF/ADAT/Word Clock;
standaloneProcessingMode = Mixer/ADAT; sampleRate = 44.1..192 kHz.

### 1d. Menu do painel frontal [lido para as strings; ligação = hipótese forte]
No bloco de UI do HD 8 (arquivo 0x403eb3..0x405053), o "Global Settings" tem estes rótulos:
Display Brightness, LED Brightness, Clock Source, Sample Rate, Standalone, **Reamp Out**, Mobile Mode,
Reset Settings. As chaves ficam lado a lado (`reampOutLabel` … `reampOutSource`).
O HD 2 tem medidores `reampOut1/2` mas **não tem** `reampOutLabel`: o menu "Reamp Out" existe só no HD 8.

### 1e. O que o firmware faz com o id 35 [lido]
- O setter do HD 8 é um override na vtable 0x3012e1c0, slot 0x3027dc09. Função 0x3027dc08:
  `switch(id-9)`, com tabela em 0x3027dc1c.
  - id 34 (headphoneSource): grava em [obj+0x894] e notifica o host com `PaRi` sel **8**.
  - id 36 (spdifSource): grava em [obj+0x129dd] e notifica o host com `PaRi` sel **11**.
  - **id 35 (reampOutSource)** (0x3027dcfa): grava em **[obj+0x129e4]** e chama
    `0x3028c158(obj, sel=0xC, valor=[obj+0x129dc])`, que notifica o host com `PaRi` global **sel 12**.
    Estranho: o valor enviado vem do byte 0x129dc, e não do novo valor. No caminho de restauração
    de settings (0x3027d5c0) o valor enviado no sel 12 é o próprio [0x9e4].
- Leitores de [obj+0x129e4]: o getter (0x3027cf5a), o serializador de settings (0x3027d470) e o
  loader (0x3027d5c2). Nenhum outro acesso direto. O efeito no roteamento de DSP deve vir de um
  listener genérico de "param id mudou" (`0x302aeabc`), que eu **não localizei**. **[hipótese]**

## 2. aux/ch13-14 = `QuantumUsbHD8DawOutputComponent`

- XML `quantumusbdefs.xml`, template `QuantumHd8`: aux ch1-4 AnalogOutput, ch5-8 SmuxADATOutput,
  ch9-12 ADATOutput, ch13-14 HD8DawOutput. Comentário no XML: "4x Analog, 8x ADAT, 2x USB". **[lido]**
- Função de nome do barramento no plugin: 0x6ff0. Com tipo=4 (saída) e subtipo=3 (DAW/USB), usa
  `"Loopback %d"` (string 0x58b22d, xref 0x7210). Os subtipos 0 e 2 usam `"Out %d/%d"` (2i+3, 2i+4)
  e `"ADAT %d/%d"`. **[lido]** Resultado: aux 13/14 = **Loopback 1 / Loopback 2**.
- A diferença entre `QuantumUsbHD8DawOutputComponent` (fábrica 0x125e4) e
  `QuantumUsbDawOutputComponent` (0x121bc): só a constante em [obj+0xf8] =
  **0x47bb8000 (96000.0)** em vez de 0x483b8000 (192000.0). A vtable muda só nos destrutores. **[lido]**
  Leitura provável: o loopback do HD 8 só existe até 96 kHz. **[hipótese]**
- Não há ligação desses barramentos com as saídas re-amp. **[lido]**

## 3. Comandos: existe rota mixer → re-amp?

### 3a. Formato da mensagem (plugin) [lido]
- Envio de int: `0x1a6f8`, que chama `0x1a390` (fila, com coalescência em `0x1aac4`) e depois o
  transporte [dev+0x70]->vtbl+0x38. Pacote de 20 bytes:
  `u32 'PaRi' (0x50617269, bytes "iraP") | u32 len=0x14 | u32 grupo | u32 seletor | i32 valor`.
  Float: `0x1a794`, com 'PaRa' (0x50615261). O cabeçalho vem de 0xa0658/0xa0660.
- Outras FourCC em 0xa0650..: 'ASet'(0x38), 'MLff'(0x294), 'MBdf'(0x1f4), 'mpro'/'mprm'(0x1f0).
  São listas em lote (níveis de mixer), com builders em 0x1b0d4 e 0x1ad44. Não decodifiquei.
- Transporte físico: bulk da Thesycon (`OpenBulkCmdDevice`, `SendBulkData`, `WriteBulkDataSync`).
  Não rastreei a cadeia inteira. **[hipótese]**

### 3b. Seletores globais que o plugin usa [lido]
Tabela de binding em 0x51c4.. e handler de mudança em 0x5ac8 / 0x6044. Tags do ParamList `Global` → sel:

| tag XML | param | sel `PaRi`/`PaRa` |
|---|---|---|
| 0 | mainOutVolume | 2 (float) |
| 3 | phones1_volume | 7 (float) |
| 5 | muteButtonMode | 1 |
| 6 | dimLevel | 3 (float) |
| 10 | ledBrightness | 15 |
| 51 | phones2_volume | 9 (float) |
| 2 | phones1_src | **8** |
| 50 | phones2_src | **10** |
| 4 | spdifSource | **11** |

Valor de origem (0x6044): 0=Main L/R, 1..4=Out 3/4..9/10, 5..12=ADAT 1/2..15/16 (barramentos),
13..14=Loopback 1/2, 15="S/PDIF Out". Bate com os máximos do firmware (headphoneSource 0..14,
spdifSource 0..15). **O plugin não tem seletor de re-amp.** Não existe string "reamp" no
`__cstring` do plugin, em `quantumdevice.dylib`, no `ucdaemon` nem no XML. **[lido]**

### 3c. Handler de escrita no firmware (host→device) [lido]
`0x3028ceb4`: `if (sel > 15) return; switch (sel)`, com tabela tbb em 0x3028cec6:

| sel | ação (id de firmware) |
|---|---|
| 0 | 12 muteState |
| 1 | 13 muteMode |
| 4 | 15 mainEncoderLink |
| 5 | 37 speakerSelectMode |
| 6 | 38 speakerSelected |
| 8 | headphoneSelect:=0, 34 headphoneSource:=v, restaura (phones 1) |
| 10 | headphoneSelect:=1, 34:=v, restaura (phones 2) |
| 11 | 36 spdifSource |
| 14 | [obj+0x90c]:=v |
| 15 | 2 ledBrightness |
| 2,3,7,9,**12**,13 | **no-op** |

A vtable do HD 8 (0x3012e1c0) aponta **para esta mesma função base** (0x3028ceb5, slot 18).
Não há override. Então `PaRi` global sel 12 enviado pelo host **não altera** `reampOutSource`.
Nenhum ponto do firmware chama o setter genérico `0x302ae6b8` com id imediato 0x23. Só há
caminhos com id variável: a edição por encoder/menu (`0x302ae8c0`) e o loader de settings.
**[lido]**, confiança média-alta. Ressalva: pode existir outro caminho de entrada (ex.: SysEx na
porta MIDI "Quantum HD 8 Control") que não investiguei.

## 4. Confiança por afirmação

| # | afirmação | tipo | confiança |
|---|---|---|---|
| A | "Reamp 1/2" = nomes das saídas USB 11/12 no firmware | lido | alta |
| B | `reampOutSource` = id 35, enum com 8 opções ADAT 1/2..15/16 | lido | alta |
| C | "Reamp Out" é item do menu Global Settings do painel do HD 8 | lido (strings) | alta |
| D | aux ch13-14 = Loopback 1/2; nada a ver com re-amp | lido | alta |
| E | o mixer do UC (14 aux + main) não tem destino re-amp; o plugin não envia rota de re-amp | lido | alta |
| F | o firmware reporta o re-amp no `PaRi` sel 12 e ignora escrita do host no sel 12 | lido | média-alta |
| G | o valor reportado no sel 12 pelo setter vem de [0x129dc] (parece bug) | lido | média |
| H | "ADAT k/k+1" = barramento de saída ADAT do mixer (logo mix→re-amp via aux ch4+k) | hipótese | baixa-média |
| H' | alternativa: é o par de **entrada** ADAT (uso standalone "ADAT") | hipótese | baixa-média |
| I | HD8DawOutput limita o loopback a ≤96 kHz | hipótese | média |

## 5. Próximo passo sugerido (medir, sem escrever nada novo pelo host)

1. Pelo painel frontal: Global Settings > Reamp Out. Anotar o valor atual.
2. Com medidores reais (`openrig://meters`, meters do UC), tocar um sinal só no USB 11/12 e ver se
   ele sai no re-amp. Depois mandar sinal só para `aux/ch(4+k)` e ver se aparece no re-amp. Isso
   decide entre H e H'.
3. Escutar a notificação `PaRi` sel 12 ao mudar no painel. Isso confirma F sem escrever nada.

## 6. Medição 19/09 (cabo Re-amp 1 → In 3, medidores calibrados, MEDIANA sobre 1,5 s)

Executado o passo 5 acima. João leu o painel frontal diretamente: **Global Settings > Reamp Out =
ADAT 1/2** neste aparelho (não ADAT 3/4 — a leitura anterior estava errada, ver "conclusão
errada" abaixo).

**(1) USB 11 → Re-amp 1, Mixer Bypass vs. Analog + ADAT:** tom 0,05 no `line/ch27` (USB 11), lido
em `In 3` (pré 19,5 dB) via o cabo Re-amp 1 → In 3. Com `global/mixerMode` = "Mixer Bypass" **e**
com "Analog + ADAT", `In 3` mediu **-40,4 dBFS** nos dois casos (confirmado duas vezes) -- o
caminho USB 11/12 → Re-amp independe do modo do mixer (o firmware, não o mixer do UC, decide essa
rota; ver §1, §3). **Confirmado, alta confiança.**

**(2) Mixer ligado ("Analog + ADAT"), tom só no aux/ch1 (Out 3/4):** tom 0,05 enviado **apenas**
para `aux/ch1` (`line/ch30/aux1`). Medidor do próprio `aux1` marcou -26,0 dBFS (sinal presente no
bus). `In 3` (via Re-amp 1) marcou **-90,3 dBFS = piso de ruído** -- **não chega** ao re-amp.

**(3) Mixer ligado, tom só no aux/ch6 (bus ADAT 3/4, nome confirmado pelo `chnum` do
dispositivo):** tom enviado **apenas** para `aux/ch6`. Medidor do `aux6` marcou -26,0 dBFS. `In 3`
marcou **-90,3 dBFS = piso de ruído** -- **não chega** ao re-amp.

**(4) Mixer ligado, tom só no aux/ch5 (bus ADAT 1/2 = ajuste do painel, lido pelo jpfaria),
com o OK dele (alimenta o Ampero):** medidor do `aux5` −26,0 dBFS; `In 3` **−96,3 dBFS** (piso).
**Não chega.** H (Reamp Out = barramento de saída ADAT do mixer) **refutada** com o host
conectado. Resta H' (par de *entrada* ADAT) ou efeito só no modo standalone — não medido.

### Conclusão errada de 19/09 (revertida)

A tabela e o SKILL.md chegaram a afirmar "mixer aux/ADAT 3/4 alcança o Re-amp 1 a -40,4 dBFS,
painel em ADAT 3/4". Isso era um **artefato de medição**: a leitura de -40,4 dBFS em `aux/ch6`
usou o **máximo** de uma janela que ainda continha o tom anterior de USB 11 (que de fato alimenta
o re-amp, ver (1)), e ao mesmo tempo o USB 4 também estava indo para o aux1 -- duas variáveis
mudando junto, sem tempo de acomodação nem mediana. Reexecutando com mediana pós-acomodação e uma
variável por vez, (2) e (3) mostram claramente piso de ruído. **A conclusão foi revertida.**

### Conclusão (corrigida)

- O único caminho **confirmado** para o re-amp é **USB 11/12** (CoreAudio outputs 11/12).
- `Out 3/4` (aux1) e o bus `ADAT 3/4` (aux6) **não** alcançam o re-amp — refutado por medição direta.
- O painel deste aparelho está em **ADAT 1/2** (lido diretamente, não em ADAT 3/4).
- Se um mix do mixer chega ao re-amp pelo bus ADAT que o painel seleciona (H) continua **em
  aberto**, não confirmado nem refutado — só ADAT 1/2 resolveria isso e não foi testado (alimenta
  equipamento real).
- O seletor do painel (*qual* saída vai para o re-amp) continua não mudável pelo host (§3c, `PaRi`
  sel 12 ignorado na escrita).
- Lição de método: medir com **mediana** após tempo de acomodação, **uma variável por vez**; nunca
  usar máximo sobre uma janela que ainda contém a transição do teste anterior.
