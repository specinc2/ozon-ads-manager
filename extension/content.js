// Ozon Price Helper — собирает данные с карточки товара Ozon.
// Читает: название, цену, страну поставки (РФ/Китай), SKU — из DOM.
// Отправляет на сервер анализатора (POST /api/plugin/collect).

const SERVER_URLS = [
  "https://searx.dungeonverse.ru", // прод
  "http://127.0.0.1:8002",          // сервер
  "http://127.0.0.1:8000",          // локальная разработка
];

function getSku() {
  // /product/apple-iphone-15-2795954097/  -> 2795954097
  const m = location.pathname.match(/\/product\/[^/]+-(\d+)\/?$/);
  return m ? m[1] : "";
}

function getTitle() {
  // h1 на карточке товара Ozon
  const h1 = document.querySelector("h1");
  if (h1 && h1.textContent.trim()) return h1.textContent.trim();
  // fallback: title страницы
  const t = document.title.replace(/ - купить на OZON.*$/, "").trim();
  return t || "";
}

function getPrice() {
  // Попытки разных селекторов цены Ozon
  const selectors = [
    "[data-widget='webPrice'] [data-test-id='price']",
    "[data-widget='webPrice'] span",
    "[data-widget='webProductPrice'] span",
    "span[data-test-id='price']",
    ".c3017-a",
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const t = el.textContent.trim();
      const m = t.match(/([\d\s\u00a0]{2,12})\s?₽/);
      if (m) {
        const num = parseInt(m[1].replace(/[\s\u00a0]/g, ""), 10);
        if (num > 0) return num;
      }
    }
  }
  // Fallback: весь текст страницы
  const bodyText = document.body.innerText || "";
  const m = bodyText.match(/([\d\s\u00a0]{2,12})\s?₽/);
  if (m) {
    const num = parseInt(m[1].replace(/[\s\u00a0]/g, ""), 10);
    if (num > 0) return num;
  }
  return 0;
}

function getCountry() {
  // Ozon на карточке показывает страну поставки: "Торгуется из", "Россия", "Китай"
  const bodyText = document.body.innerText || "";

  // Прямые маркеры в тексте
  const markers = [
    { label: "Россия", aliases: ["россия", "из рф", "торгуется из россии"] },
    { label: "Китай", aliases: ["китай", "china", "торгуется из китая", "из-за рубежа"] },
  ];

  for (const mk of markers) {
    for (const alias of mk.aliases) {
      if (bodyText.toLowerCase().includes(alias)) return mk.label;
    }
  }

  // Ищем блок "Страна" в характеристиках (если есть)
  const countrySelectors = [
    "[data-widget='webCharacteristics']",
    "[data-widget='webShortCharacteristics']",
  ];
  for (const sel of countrySelectors) {
    const el = document.querySelector(sel);
    if (el) {
      const t = el.innerText || "";
      const m = t.match(/страна[^:]{0,4}[: ]\s*([А-Яа-яЁё]+)/i);
      if (m) return m[1];
    }
  }

  return "";
}

function collect() {
  const data = {
    url: location.href,
    sku: getSku(),
    name: getTitle(),
    price: getPrice(),
    currency: "RUB",
    country: getCountry(),
    marketplace: "ozon",
  };
  return data;
}

function send(data) {
  return new Promise((resolve) => {
    // Токен плагина из storage (настраивается в popup)
    chrome.storage.local.get(["pluginToken"], (res) => {
      const token = res.pluginToken || "";
      let idx = 0;
      const tryNext = () => {
        if (idx >= SERVER_URLS.length) return resolve(false);
        const server = SERVER_URLS[idx++];
        fetch(`${server}/api/plugin/collect?token=${encodeURIComponent(token)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Plugin-Token": token },
          body: JSON.stringify(data),
        })
          .then((r) => r.json())
          .then((j) => {
            if (j && j.ok) resolve(true);
            else tryNext();
          })
          .catch(() => tryNext());
      };
      tryNext();
    });
  });
}

// Автоматический сбор при открытии карточки (с задержкой на рендер)
let sent = false;
function autoCollect() {
  if (sent || !location.pathname.includes("/product/")) return;
  const data = collect();
  if (data.price > 0 && (data.sku || data.name)) {
    send(data).then((ok) => {
      if (ok) {
        sent = true;
        console.log("[Ozon Price Helper] Отправлено:", data.name, data.price, data.country);
      }
    });
  }
}

// Ждём рендера страницы и собираем
setTimeout(autoCollect, 3000);
window.addEventListener("load", () => setTimeout(autoCollect, 1500));
setTimeout(autoCollect, 8000); // повторная попытка, если цена появилась позже
