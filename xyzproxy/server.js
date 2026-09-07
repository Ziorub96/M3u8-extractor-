import express from "express";
import vm from "vm";
import { fetch as undiciFetch, Agent } from "undici";
import { Readable } from "stream";
import { pipeline } from "stream/promises";

const app = express();
const PORT = process.env.PORT || 3000;

// Agent undici per ignorare certificati SSL errati/scaduti
const dispatcher = new Agent({
  connect: {
    rejectUnauthorized: false
  }
});

async function fetchWithAgent(url, options = {}) {
  return undiciFetch(url, { ...options, dispatcher });
}

// Whitelist per mitigare SSRF
const ALLOWED_HOSTS = [
  "player.xyzstreams.st",
  "xyzstreams.st",
  "xyzstreams-6h9.pages.dev",
  "mpd26wc64.blogspot.com",
  "krxplor.github.io"
];

function isHostAllowed(targetUrl) {
  try {
    const parsed = new URL(targetUrl);
    return ALLOWED_HOSTS.some(host => parsed.hostname === host || parsed.hostname.endsWith("." + host));
  } catch {
    return false;
  }
}

const STREAM_REFERER = "https://xyzstreams-6h9.pages.dev/worldcup26-1-0710";
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

const resolvedUrlsCache = {};
const CACHE_DURATION = 10 * 60 * 1000;

let cachedHindiUrl = "https://mpd26wc64.blogspot.com/p/matchday01.html";
let lastHindiScrape = 0;
const CACHE_TTL = 3 * 60 * 1000;

async function resolveStreamUrl(channel) {
  const embedId = EMBED_MAP[channel];
  if (!embedId) throw new Error(`Canale ${channel} non mappato`);

  const cached = resolvedUrlsCache[channel];
  if (cached && Date.now() < cached.expiresAt) return cached.url;

  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const embedUrl = `https://player.xyzstreams.st/embed/${embedId}`;
      const res = await fetchWithAgent(embedUrl, {
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
        clearInterval: () => {}
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
            const dummyFunc = () => dummyProxy;
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
      // Timeout per evitare cicli infiniti/blocco server
      vm.runInContext(scriptContent, context, { timeout: 2000 });

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

async function getHindiUrl() {
  if (Date.now() - lastHindiScrape < CACHE_TTL) return cachedHindiUrl;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetchWithAgent("https://mpd26wc64.blogspot.com/p/matchday01.html", {
        headers: { "User-Agent": "Mozilla/5.0" },
        signal: AbortSignal.timeout(5000)
      });
      if (res.status === 429) {
        await new Promise(r => setTimeout(r, 2000 * attempt));
        continue;
      }
      if (res.ok) {
        const html = await res.text();
        const match = html.match(/src="(https:\/\/krxplor\.github\.io\/plyr\/\?x=[^"]+)"/i);
        if (match?.[1]) {
          cachedHindiUrl = match[1];
          lastHindiScrape = Date.now();
          return cachedHindiUrl;
        }
      }
    } catch (e) {
      console.error(`Hindi error: ${e}`);
    }
  }
  return cachedHindiUrl;
}

function rewriteM3u8(text, baseUrl) {
  const lines = text.split('\n');
  return lines.map(line => {
    const trimmed = line.trim();
    if (!trimmed) return line;
    if (trimmed.startsWith('#')) {
      return trimmed.replace(/URI=["']([^"']+)["']/g, (m, uri) => {
        const fullUri = new URL(uri, baseUrl).href;
        return `URI="/api/proxy/segment?url=${encodeURIComponent(fullUri)}"`;
      });
    }
    const fullUrl = new URL(trimmed, baseUrl).href;
    return `/api/proxy/segment?url=${encodeURIComponent(fullUrl)}`;
  }).join('\n');
}

app.get("/api/proxy/stream/:channel", async (req, res) => {
  try {
    const channel = req.params.channel.replace('.m3u8', '');
    const streamUrl = (channel === 'hindi') ? await getHindiUrl() : await resolveStreamUrl(channel);

    const response = await fetchWithAgent(streamUrl, {
      headers: {
        "Referer": PLAYER_REFERER,
        "Origin": PLAYER_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
      }
    });

    if (!response.ok) return res.status(response.status).send("Failed to fetch stream");

    res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
    res.setHeader("Access-Control-Allow-Origin", "*");

    const text = await response.text();
    const rewritten = rewriteM3u8(text, streamUrl);
    res.send(rewritten);
  } catch (error) {
    console.error("Stream proxy error:", error);
    res.status(500).json({ error: "Failed to proxy stream" });
  }
});

app.get("/api/proxy/segment", async (req, res) => {
  try {
    const segmentUrl = req.query.url;
    if (!segmentUrl) return res.status(400).send("Missing url parameter");

    if (!isHostAllowed(segmentUrl)) {
      return res.status(403).send("Host non autorizzato");
    }

    const response = await fetchWithAgent(segmentUrl, {
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

    if (segmentUrl.includes('.m3u8') || contentType?.includes("mpegurl")) {
      const text = await response.text();
      const rewritten = rewriteM3u8(text, segmentUrl);
      res.setHeader("Content-Type", "application/vnd.apple.mpegurl");
      res.send(rewritten);
    } else {
      // Streaming con pipeline per gestire disconnessioni client
      if (response.body) {
        try {
          await pipeline(Readable.fromWeb(response.body), res);
        } catch (err) {
          // Client disconnesso o errore di trasmissione: non far crashare il server
          console.error("Pipeline error:", err);
          if (!res.headersSent) {
            res.status(500).send("Stream error");
          }
        }
      } else {
        res.status(500).send("Stream body non disponibile");
      }
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