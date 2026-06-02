# Firmware (PlatformIO + VSCode)

Локальний білд ESP32-прошивки через PlatformIO. 

## Що потрібно встановити

- [VSCode](https://code.visualstudio.com/) + розширення **PlatformIO IDE**
  (`platformio.platformio-ide`)
- Розширення **Wokwi Simulator** для VSCode (`wokwi.wokwi-vscode`) +
  безкоштовний токен з [wokwi.com/dashboard](https://wokwi.com/dashboard)
- Docker (для Django + Postgres локально)

- При першому відкритті папки VSCode сам запропонує поставити рекомендоване розширення з .vscode/extensions.json

## Перший запуск

1. **Скопіюйте шаблон секретів** і заповни значеннями:

   ```powershell
   Copy-Item src/secrets.h.example src/secrets.h
   ```

   У `src/secrets.h` пропишите `API_KEY_1..3` - потрібен окремий ключ на кожен прилад,
   рівний полю `api_key` відповідного `Device` в адмінці. 
   Решту полів (`SERVER_URL`, `WIFI_*`) можна залишити дефолтними.

2. **Підніміть Django** у корені проекту:

   ```powershell
   docker compose up -d
   ```

   Перевірте, що `http://localhost:8000/admin/` відкривається.

3. **Зберіть прошивку** у VSCode:
   `Ctrl+Shift+P` → `PlatformIO: Build` (або кнопка ✓ внизу).

4. **Запустіть симуляцію**:
   Відкрийте `diagram.json` → права кнопка → `Start Wokwi Simulator`.
   Розширення підхопить `wokwi.toml`, схему і зібраний бінарник із `.pio/`.



## Структура

```
firmware_esp32/
├── platformio.ini         # PIO конфіг (платформа, бібліотеки)
├── diagram.json           # Wokwi-схема (ESP32 + поти + LED + OLED)
├── wokwi.toml             # шлях до бінарника для симуляції
├── src/
│   ├── main.cpp           # код прошивки
│   ├── secrets.h.example  # шаблон секретів (комітиться)
│   └── secrets.h          # реальні токени
└── .gitignore             # ігнорує .pio/ та машинні файли VSCode
```

## Часті проблеми

- **`secrets.h: No such file or directory`** — забув скопіювати з
  `secrets.h.example`.
- **HTTP 403 на uplink/downlink** — ключ приладу (`API_KEY_1..3` у `secrets.h`)
  не співпадає з `api_key` цього приладу в адмінці, або `device_id` не існує
  в БД, також, якщо є невідомий пристрій - він також дає 403.
- **HTTP -1 / timeout** — Django не запущений; перевір `docker compose ps`
  і чи слухає `localhost:8000`.
- **OLED темний** — у `diagram.json` дефолтна I²C-адреса `0x3C`; деякі
  модулі мають `0x3D`.
