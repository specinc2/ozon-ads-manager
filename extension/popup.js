// Ozon Price Helper — popup: сохранение токена плагина.
document.addEventListener("DOMContentLoaded", () => {
  const tokenInput = document.getElementById("token");
  const saveBtn = document.getElementById("save");
  const status = document.getElementById("status");

  chrome.storage.local.get(["pluginToken"], (res) => {
    if (res.pluginToken) tokenInput.value = res.pluginToken;
  });

  saveBtn.addEventListener("click", () => {
    const token = tokenInput.value.trim();
    if (!token) {
      status.textContent = "Введите токен";
      status.style.color = "#c0392b";
      return;
    }
    chrome.storage.local.set({ pluginToken: token }, () => {
      status.textContent = "Сохранено ✓";
      status.style.color = "#0a7d32";
    });
  });
});
