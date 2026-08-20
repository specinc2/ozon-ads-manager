const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('/tmp/challenge.html', 'utf-8');

const dom = new JSDOM(html, {
    url: 'https://www.ozon.ru/challenge.html',
    referrer: 'https://www.ozon.ru/',
    contentType: 'text/html',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
});

const win = dom.window;
const doc = win.document;

// Подавляем console скрипта (огромные объекты)
win.console = {
    log: function(){}, warn: function(){}, error: function(){}, info: function(){}, debug: function(){},
};

win.navigator = {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    platform: 'Win32',
    language: 'ru-RU',
    languages: ['ru-RU', 'ru'],
    cookieEnabled: true,
    webdriver: false,
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    vendor: 'Google Inc.',
    product: 'Gecko',
    appVersion: '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    plugins: { length: 5, item: () => null, namedItem: () => null },
    mimeTypes: { length: 4 },
    webkitGetUserMedia: function(){},
    onLine: true,
    permissions: { query: () => Promise.resolve({ state: 'granted' }) },
    connection: { effectiveType: '4g', rtt: 50, downlink: 10 },
    storage: { estimate: () => Promise.resolve({ usage: 1000, quota: 1000000 }) },
};

function makeGL() {
    const noop = function(){};
    const v = {
        7938: 'WebKit WebGL', 7937: 'WebGL', 37445: 'Google Inc. (NVIDIA)',
        37446: 'ANGLE (NVIDIA, NVIDIA GeForce GTX)', 3379: 128, 34076: 16384,
        34024: 1024, 36347: 1024, 34921: 128,
    };
    return {
        getParameter: function(p) { return v[p] !== undefined ? v[p] : 0; },
        getExtension: function() { return null; },
        getSupportedExtensions: function() { return ['WEBGL_debug_renderer_info']; },
        getContextAttributes: function() { return { antialias: true, depth: true, alpha: true }; },
        createBuffer: noop, bindBuffer: noop, bufferData: noop, bufferSubData: noop,
        createShader: function(){ return {}; }, shaderSource: noop, compileShader: function(){ return true; },
        getShaderParameter: function(){ return true; }, createProgram: function(){ return {}; },
        attachShader: noop, linkProgram: noop, getProgramParameter: function(){ return true; },
        useProgram: noop, getAttribLocation: function(){ return 0; }, getUniformLocation: function(){ return {}; },
        uniform1f: noop, uniform2f: noop, uniform3f: noop, uniform4f: noop,
        uniform1i: noop, uniform2i: noop, uniform3i: noop, uniform4i: noop,
        createTexture: noop, bindTexture: noop, texImage2D: noop, texParameteri: noop,
        activeTexture: noop, enable: noop, disable: noop, clearColor: noop, clear: noop,
        viewport: noop, drawArrays: noop, drawElements: noop, flush: noop, finish: noop,
        isContextLost: function(){ return false; },
    };
}

const origCreateElement = doc.createElement.bind(doc);
doc.createElement = function(tag) {
    const el = origCreateElement(tag);
    if (tag === 'canvas') {
        el.getContext = function(type) {
            if (type === 'webgl' || type === 'experimental-webgl') {
                return makeGL();
            }
            return {
                canvas: el,
                fillRect: function(){}, strokeRect: function(){}, clearRect: function(){},
                fillText: function(){}, strokeText: function(){},
                measureText: function(t){ return { width: String(t).length * 8 }; },
                getImageData: function(){ return { data: new Uint8ClampedArray(100) }; },
                createLinearGradient: function(){ return { addColorStop: function(){} }; },
                createRadialGradient: function(){ return { addColorStop: function(){} }; },
                font: '', textAlign: '', textBaseline: '', fillStyle: '', strokeStyle: '',
                beginPath: function(){}, closePath: function(){}, moveTo: function(){}, lineTo: function(){},
                arc: function(){}, arcTo: function(){}, bezierCurveTo: function(){}, quadraticCurveTo: function(){},
                rect: function(){}, fill: function(){}, stroke: function(){}, clip: function(){},
                translate: function(){}, scale: function(){}, rotate: function(){}, transform: function(){}, setTransform: function(){},
                save: function(){}, restore: function(){}, globalAlpha: 1, globalCompositeOperation: 'source-over',
            };
        };
        el.toDataURL = function(){ return 'data:image/png;base64,iVBORw0KGgo='; };
        el.width = 300; el.height = 150;
    }
    return el;
};

let capturedPost = null;
win.fetch = async function(url, options) {
    if (String(url).includes('/abt/result')) {
        capturedPost = {
            url: String(url),
            method: options ? options.method : 'POST',
            body: options ? options.body : null,
            headers: options ? options.headers : {},
        };
        console.log('=== ПЕРЕХВАЧЕН POST на /abt/result ===');
        console.log('body:', String(capturedPost.body).substring(0, 300));
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }), text: async () => 'ok' };
    }
    return { ok: false, status: 404, json: async () => ({}), text: async () => '' };
};
win.XMLHttpRequest = function(){ this.open=function(){}; this.send=function(){}; this.setRequestHeader=function(){}; };

// Performance как Proxy: любое свойство имеет toJSON
const perfTarget = {
    timing: {
        navigationStart: Date.now()-5000, loadEventEnd: Date.now(),
        toJSON: function(){ return { navigationStart: this.navigationStart, loadEventEnd: this.loadEventEnd }; },
    },
    getEntries: function(){ return []; },
    getEntriesByName: function(){ return [{ startTime: 100 }]; },
    getEntriesByType: function(){ return []; },
    mark: function(){}, measure: function(){}, now: function(){ return Date.now(); },
    timeOrigin: Date.now() - 5000,
};
win.performance = new Proxy(perfTarget, {
    get(target, prop) {
        if (prop in target) return target[prop];
        // Любое неизвестное свойство — объект с toJSON
        const obj = {
            toJSON: function(){ return {}; },
            getEntries: function(){ return []; },
        };
        return obj;
    }
});
win.msCrypto = win.crypto;

const scripts = [...doc.querySelectorAll('script')];
console.log('Скриптов в HTML:', scripts.length);
for (const s of scripts) {
    const code = s.textContent;
    if (code && code.length > 1000) {
        console.log('Выполняю основной скрипт, len:', code.length);
        try {
            win.eval(code);
        } catch (e) {
            console.log('Ошибка выполнения:', e.message.substring(0, 150));
        }
    }
}

setTimeout(() => {
    console.log('\n=== РЕЗУЛЬТАТ ===');
    console.log('POST перехвачен:', !!capturedPost);
    if (capturedPost) {
        console.log('Полное тело POST (первые 1000):');
        console.log(String(capturedPost.body).substring(0, 1000));
    } else {
        console.log('POST не перехвачен. Куки:', doc.cookie);
        const cd = doc.querySelector('.challenge-data');
        console.log('challenge-data текст:', cd ? cd.textContent : 'нет');
    }
    process.exit(0);
}, 10000);
