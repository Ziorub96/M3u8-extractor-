import express from "express";
import vm from "vm";

const app = express();
const PORT = process.env.PORT || 3000;

// Disabilita la verifica TLS per superare il certificato non valido
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

// ================== CONFIGURAZIONE ==================
const STREAM_REFERER = "https://xyzstreams-6h9.pages.dev/worldcup26-1-0710";
const STREAM_ORIGIN = "https://xyzstreams-6h9.pages.dev";
const PLAYER_REFERER = "https://player.xyzstreams.st/";
const PLAYER_ORIGIN = "https://player.xyzstreams.st";

const EMBED_MAP = {
  "fox": "fox-xyz-waUvqaAA",
  "fox4k": "fox4k-usa",
  "bbc": "bbcone-uk",
  "tsn": "tsn1-xyz-waUvqaAACr",
  "tsn4k": "tsn4k-xyz-waUvqaAACr",
  "bein": "bein-xyz-waUvqaAAC",
  "bein4k": "bein4k-xyz-waUvqaAAC",
  "beinfr": "bein12fr-xyz",
  "telemundo": "telemundo-xyz-waUvqaAACr",
  "telemundo4k": "telemundo-xyz-waUvqaAACr",
  "fussball4k": "fussballtv1uhd-de"
};

const FALLBACK_MAP = {
  "fox": "https://xyzstreams.st/wc-1-embed.html",
  "fox4k": "https://xyzstreams.st/wc-5-embed.html",
  "bbc": "https://xyzstreams.st/wc-3-embed.html",
  "tsn": "https://xyzstreams.st/wc-7-embed.html",
  "tsn4k": "https://xyzstreams.st/wc-8-embed.html",
  "bein": "https://xyzstreams.st/wc-17-embed.html",
  "bein4k": "https://xyzstreams.st/wc-10-embed.html",
  "beinfr": "https://xyzstreams.st/wc-22-embed.html",
  "telemundo": "https://xyzstreams.st/wc-6-embed.html",
  "telemundo4k": "https://xyzstreams.st/wc-6-embed.html",
  "fussball4k": "https://xyzstreams.st/wc-14-embed.html"
};

const resolvedUrlsCache = {};
const CACHE_DURATION = 10 * 60 * 1000;

let cachedLiveMap = {};
let lastScrapeTime = 0;
let isScraping = false;
const CACHE_TTL = 3 * 60 * 1000;

let cachedHindiUrl = "https://mpd26wc64.blogspot.com/p/matchday01.html";
let lastHindiScrape = 0;

async function resolveStreamUrl(channel) {
  const embedId = EMBED_MAP[channel];
  if (!embedId) {
    throw new Error(`Canale ${channel} non mappato`);
  }

  const cached = resolvedUrlsCache[channel];
  if (cached && Date.now() < cached.expiresAt) return cached.url;

  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const embedUrl = `https://player.xyzstreams.st/embed/${embedId}`;
      const res = await fetch(embedUrl, {
        signal: AbortSignal.timeout(5000),
        headers: {
          "Referer": STREAM_REFERER,
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const html = await res.text();

      const scriptStart = html.indexOf("(function(_0x");
      if (scriptStart === -1) throw new Error("Pattern script non trovato");
      const openTagIndex = html.lastIndexOf("<script", scriptStart);
      const closeTagIndex = html.indexOf("</script>", scriptStart);
      if (openTagIndex === -1 || closeTagIndex === -1) throw new Error("Tag script non trovati");
      const contentStart = html.indexOf(">", openTagIndex) + 1;
      const scriptContent = html.substring(contentStart, closeTagIndex);

      let playerOptions = null;

      const mockWindow = {
        location: { href: embedUrl, hostname: "player.xyzstreams.st", pathname: `/embed/${embedId}`, search: "" },
        navigator: { userAgent: "Mozilla/5.0" },
        setTimeout: () => {},
        setInterval: () => {},
        clearTimeout: () => {},
        clearInterval: () => {},
        console: { log: () => {}, error: () => {} },
        eval: eval
      };

      const mockDocument = {
        referrer: STREAM_REFERER,
        getElementById: () => ({ addEventListener: () => {}, classList: { add: () => {}, remove: () => {} }, style: {} }),
        addEventListener: () => {},
        createElement: () => ({ setAttribute: () => {}, appendChild: () => {}, style: {} }),
        querySelector: () => null,
        querySelectorAll: () => []
      };

      const context = {
        window: mockWindow,
        document: mockDocument,
        navigator: mockWindow.navigator,
        location: mockWindow.location,
        setTimeout: mockWindow.setTimeout,
        setInterval: mockWindow.setInterval,
        clearTimeout: mockWindow.clearTimeout,
        clearInterval: mockWindow.clearInterval,
        console: { log: () => {}, error: () => {} },
        Clappr: new Proxy({
          Player: function(options) {
            playerOptions = options;
            const dummyFunc = (...args) => dummyProxy;
            const dummyProxy = new Proxy(dummyFunc, {
              get: (target, prop) => {
                if (prop === 'options') return options;
                if (prop === 'then') return undefined;
                return (...args) => {
                  if (prop === 'configure' || prop === 'load') {
                    if (typeof args[0] === 'string') playerOptions.source = args[0];
                    else if (args[0] && typeof args[0] === 'object') playerOptions = Object.assign(playerOptions || {}, args[0]);
                  }
                  return dummyProxy;
                };
              }
            });
            return dummyProxy;
          }
        }, {
          get: (target, prop) => {
            if (prop in target) return target[prop];
            return new Proxy({}, { get: (t, p) => p });
          }
        })
      };
      context.window.window = context;
      context.window.document = mockDocument;

      vm.createContext(context);
      vm.runInContext(scriptContent, context);

      const streamUrl = playerOptions?.source;
      if (!streamUrl) throw new Error("URL stream non estratta");

      resolvedUrlsCache[channel] = { url: streamUrl, expiresAt: Date.now() + CACHE_DURATION };
      return streamUrl;
    } catch (err) {
      lastError = err;
      await new Promise(r => setTimeout(r, 150));
    }
  }
  throw lastError || new Error(`Risoluzione fallita per ${channel}`);
}

// ================== ROUTE API ==================

app.get("/api/live-channels", async (req, res) => {
  try {
    if (Date.now() - lastScrapeTime > CACHE_TTL && !isScraping) {
      isScraping = true;
      setTimeout(async () => {
        try {
          cachedLiveMap = await scrapeLiveChannels();
          lastScrapeTime = Date.now();
        } catch (e) { console.error(e); }
        isScraping = false;
      }, 0);
    }
    const result = {};
    for (const key of Object.keys(FALLBACK_MAP)) {
      result[key] = cachedLiveMap[key] || FALLBACK_MAP[key];
    }
    result["hindi"] = await getHindiUrl();
    res.json(result);
  } catch (e) {
    res.json({ ...FALLBACK_MAP, hindi: cachedHindiUrl });
  }
});

async function scrapeLiveChannels() {
  const map = {};
  try {
    const homeRes = await fetch("https://timstreams.live/", {
      headers: { "User-Agent": "Mozilla/5.0" }
    });
    if (!homeRes.ok) return map;
    const homeHtml = await homeRes.text();
    const matchRegex = /\/match\/[a-zA-Z0-9-]+/g;
    const matches = Array.from(new Set(homeHtml.match(matchRegex) || []));
    await Promise.all(matches.map(async (matchPath) => {
      try {
        const matchRes = await fetch(`https://timstreams.live${matchPath}`, {
          headers: { "User-Agent": "Mozilla/5.0" }
        });
        if (!matchRes.ok) return;
        const matchHtml = await matchRes.text();
        const watchRegex = /\/watch\/([a-zA-Z0-9-]+)/g;
        let m;
        while ((m = watchRegex.exec(matchHtml)) !== null) {
          const watchPath = m[1].toLowerCase();
          let channelKey = "";
          if (watchPath.startsWith("fox-4k")) channelKey = "fox4k";
          else if (watchPath.startsWith("fox")) channelKey = "fox";
          else if (watchPath.startsWith("bbc") || watchPath.startsWith("uk")) channelKey = "bbc";
          else if (watchPath.startsWith("tsn4k")) channelKey = "tsn4k";
          else if (watchPath.startsWith("tsn")) channelKey = "tsn";
          else if (watchPath.startsWith("bein4k") || watchPath.startsWith("bein-sports-4k")) channelKey = "bein4k";
          else if (watchPath.startsWith("bein")) channelKey = "bein";
          else if (watchPath.startsWith("telemundo-4k")) channelKey = "telemundo4k";
          else if (watchPath.startsWith("telemundo")) channelKey = "telemundo";
          else if (watchPath.startsWith("fussball")) channelKey = "fussball4k";
          if (channelKey && !map[channelKey]) {
            map[channelKey] = `https://www.timstreams.one/embed/${watchPath}`;
          }
        }
      } catch (e) {}
    }));
  } catch (e) {}
  return map;
}

async function getHindiUrl() {
  if (Date.now() - lastHindiScrape < CACHE_TTL) return cachedHindiUrl;
  try {
    const res = await fetch("https://mpd26wc64.blogspot.com/p/matchday01.html", {
      headers: { "User-Agent": "Mozilla/5.0" },
      signal: AbortSignal.timeout(5000)
    });
    if (res.ok) {
      const html = await res.text();
      const match = html.match(/src="(https:\/\/krxplor\.github\.io\/plyr\/\?x=[^"]+)"/i);
      if (match?.[1]) {
        cachedHindiUrl = match[1];
        lastHindiScrape = Date.now();
      }
    }
  } catch (e) {}
  return cachedHindiUrl;
}

app.get("/api/proxy/stream/:channel", async (req, res) => {
  try {
    const channel = req.params.channel.replace('.m3u8', '');
    let streamUrl;
    if (channel === 'hindi') {
      streamUrl = await getHindiUrl();
    } else {
      streamUrl = await resolveStreamUrl(channel);
    }

    const response = await fetch(streamUrl, {
      headers: {
        "Referer": PLAYER_REFERER,
        "Origin": PLAYER_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
      }
    });
    if (!response.ok) return res.status(response.status).send("Failed to fetch stream");
    res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
    res.setHeader("Access-Control-Allow-Origin", "*");
    let text = await response.text();
    const baseUrl = streamUrl.substring(0, streamUrl.lastIndexOf('/') + 1);
    const lines = text.split('\n');
    const rewritten = lines.map(line => {
      const trimmed = line.trim();
      if (!trimmed) return line;
      if (trimmed.startsWith('#')) {
        return trimmed.replace(/URI=["'](https?:\/\/[^"']+)["']/g, (m, uri) => `URI="/api/proxy/segment?url=${encodeURIComponent(uri)}"`);
      }
      let fullUrl = trimmed;
      if (!trimmed.startsWith('http://') && !trimmed.startsWith('https://')) fullUrl = baseUrl + trimmed;
      return `/api/proxy/segment?url=${encodeURIComponent(fullUrl)}`;
    });
    res.send(rewritten.join('\n'));
  } catch (error) {
    console.error("Stream proxy error:", error);
    res.status(500).json({ error: "Failed to proxy stream" });
  }
});

app.get("/api/proxy/segment", async (req, res) => {
  try {
    const segmentUrl = req.query.url;
    if (!segmentUrl) return res.status(400).send("Missing url parameter");
    const response = await fetch(segmentUrl, {
      headers: {
        "Referer": PLAYER_REFERER,
        "Origin": PLAYER_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
      }
    });
    if (!response.ok) return res.status(response.status).send("Failed to fetch segment");
    const contentType = response.headers.get("content-type");
    if (contentType) res.setHeader("Content-Type", contentType);
    res.setHeader("Access-Control-Allow-Origin", "*");
    if (segmentUrl.includes('.m3u8')) {
      let text = await response.text();
      const baseUrl = segmentUrl.substring(0, segmentUrl.lastIndexOf('/') + 1);
      const lines = text.split('\n');
      const rewritten = lines.map(line => {
        const trimmed = line.trim();
        if (!trimmed) return line;
        if (trimmed.startsWith('#')) {
          return trimmed.replace(/URI=["'](https?:\/\/[^"']+)["']/g, (m, uri) => `URI="/api/proxy/segment?url=${encodeURIComponent(uri)}"`);
        }
        let fullUrl = trimmed;
        if (!trimmed.startsWith('http://') && !trimmed.startsWith('https://')) fullUrl = baseUrl + trimmed;
        return `/api/proxy/segment?url=${encodeURIComponent(fullUrl)}`;
      });
      res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
      res.send(rewritten.join('\n'));
    } else {
      const buffer = await response.arrayBuffer();
      res.send(Buffer.from(buffer));
    }
  } catch (error) {
    console.error("Segment proxy error:", error);
    res.status(500).send("Failed to proxy segment");
  }
});

app.get("/", (req, res) => res.send("XYZ Proxy is running"));

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Server running on port ${PORT}`);
});