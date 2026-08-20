/* Общие JS-утилиты для страниц приложения */

// Универсальный обработчик для fetch-запросов с JSON
async function postJSON(url, body = {}, method = 'POST') {
    const resp = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: method === 'GET' ? undefined : JSON.stringify(body),
    });
    return resp.json();
}

// Автоматическое скрытие alert-сообщений через 5 секунд
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.alert-dismissible').forEach(alert => {
        setTimeout(() => {
            alert.classList.remove('show');
            setTimeout(() => alert.remove(), 300);
        }, 5000);
    });
});
