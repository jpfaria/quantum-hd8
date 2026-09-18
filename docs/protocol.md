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

Nada além disso, com `clientType` `Mac`/`UniversalControl` e com ou sem
`clientOptions`. **A árvore de parâmetros da HD 8 não vem só com o subscribe.**
O pedido que o app UC faz para recebê-la ainda não é conhecido; o próximo passo
é a captura do tráfego real do UC (`tcpdump -i lo0 port 59791`, precisa de sudo).

Strings de ids de mensagem no binário `ucdaemon` (candidatas, não medidas):
`UpdateDevices`, `AudioDeviceInfoRequest`/`AudioDeviceInfoReply`,
`DevicePropertyChanged`, `DeviceUnresponsive`, `ObjectList`, `StringList`.
