const puppeteer = require('puppeteer');
const axios = require('axios');
const cheerio = require('cheerio');
const fs = require('fs');

// ============================================================
// CONFIGURAZIONE
// ============================================================
const CONFIG = {
  // URL di DAMITV (cambia se necessario)
  baseUrl: 'https://ondemand.st',
  
  // Tempo di attesa per il caricamento della pagina (ms)
  waitTimeout: 10000,
  
  // User-Agent per sembrare un browser normale
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
};

// ============================================================
// CLASSE PRINCIPALE
// ============================================================
class DamitvExtractor {
  constructor() {
    this.browser = null;
    this.page = null;
    this.liveChannels = [];
  }

  // ==========================================================
  // METODO 1: Estrai usando Puppeteer (Browser Headless)
  // ==========================================================
  async extractWithBrowser(channelName) {
    console.log(`🔍 Cerco il canale: ${channelName}...`);
    
    try {
      // Avvia il browser
      this.browser = await puppeteer.launch({
        headless: 'new', // 'new' per Chrome/Chromium recenti
        args: ['--no-sandbox', '--disable-setuid-sandbox']
      });

      this.page = await this.browser.newPage();
      
      // Imposta User-Agent
      await this.page.setUserAgent(CONFIG.userAgent);
      
      // Vai alla home di DAMITV
      console.log(`🌐 Apertura: ${CONFIG.baseUrl}`);
      await this.page.goto(CONFIG.baseUrl, {
        waitUntil: 'networkidle2',
        timeout: CONFIG.waitTimeout
      });

      // Clicca su "Live TV" se esiste
      await this.page.click('a[onclick*="livetv"]').catch(() => {});
      
      // Aspetta che la pagina si carichi
      await this.page.waitForTimeout(3000);

      // Cerca il canale nella lista
      const channelFound = await this.page.evaluate((name) => {
        // Cerca tra i bottoni dei canali
        const buttons = document.querySelectorAll('.stream-btn, .card, [onclick*="openLiveTVChannel"]');
        for (const btn of buttons) {
          const text = btn.textContent || '';
          if (text.toLowerCase().includes(name.toLowerCase())) {
            btn.click();
            return true;
          }
        }
        return false;
      }, channelName);

      if (!channelFound) {
        console.log(`❌ Canale "${channelName}" non trovato nella lista.`);
        console.log('📋 Canali disponibili:');
        const channels = await this.page.evaluate(() => {
          const items = document.querySelectorAll('.stream-btn, .card');
          return Array.from(items).map(el => el.textContent.trim()).filter(Boolean);
        });
        console.log(channels.join(', '));
        await this.browser.close();
        return null;
      }

      // Aspetta che il player si carichi
      await this.page.waitForTimeout(5000);

      // Cerca l'URL m3u8 nel DOM
      const m3u8Url = await this.page.evaluate(() => {
        // Cerca in tutti gli iframe
        const iframes = document.querySelectorAll('iframe');
        for (const iframe of iframes) {
          const src = iframe.src || '';
          if (src.includes('.m3u8') || src.includes('playlist')) {
            return src;
          }
        }

        // Cerca nel codice sorgente della pagina
        const html = document.documentElement.innerHTML;
        const matches = html.match(/https?:\/\/[^\s"']+\.m3u8/g);
        if (matches && matches.length > 0) {
          return matches[0];
        }

        return null;
      });

      await this.browser.close();

      if (m3u8Url) {
        console.log(`✅ Link m3u8 trovato: ${m3u8Url}`);
        return m3u8Url;
      } else {
        console.log('❌ Nessun link m3u8 trovato.');
        console.log('💡 Suggerimento: Assicurati che il canale stia effettivamente trasmettendo.');
        return null;
      }

    } catch (error) {
      console.error(`❌ Errore: ${error.message}`);
      if (this.browser) await this.browser.close();
      return null;
    }
  }

  // ==========================================================
  // METODO 2: Estrai usando Axios + Cheerio (Senza Browser)
  // ==========================================================
  async extractWithHttp(channelName) {
    console.log(`🔍 Cerco il canale: ${channelName} (senza browser)...`);

    try {
      // Fai una richiesta GET alla pagina
      const response = await axios.get(CONFIG.baseUrl, {
        headers: {
          'User-Agent': CONFIG.userAgent
        }
      });

      // Parsing dell'HTML
      const $ = cheerio.load(response.data);

      // Cerca nel JavaScript della pagina
      const scripts = $('script').toArray();
      let m3u8Url = null;

      for (const script of scripts) {
        const content = $(script).html() || '';
        // Cerca pattern di URL m3u8
        const matches = content.match(/https?:\/\/[^\s"']+\.m3u8/g);
        if (matches && matches.length > 0) {
          // Prendi l'ultimo match (di solito il più recente)
          m3u8Url = matches[matches.length - 1];
          break;
        }
      }

      if (m3u8Url) {
        console.log(`✅ Link m3u8 trovato: ${m3u8Url}`);
        return m3u8Url;
      } else {
        console.log('❌ Nessun link m3u8 trovato nella pagina statica.');
        console.log('💡 Suggerimento: Usa il metodo con browser (extractWithBrowser)');
        return null;
      }

    } catch (error) {
      console.error(`❌ Errore: ${error.message}`);
      return null;
    }
  }

  // ==========================================================
  // METODO 3: Usa API interna (se disponibile)
  // ==========================================================
  async extractWithApi(matchId) {
    console.log(`🔍 Cerco match ID: ${matchId}...`);

    try {
      const response = await axios.get(`${CONFIG.baseUrl}/papi/extract-url/${matchId}`, {
        headers: {
          'User-Agent': CONFIG.userAgent,
          'Referer': CONFIG.baseUrl
        }
      });

      if (response.data && response.data.success) {
        const hlsUrl = response.data.hlsUrl;
        if (hlsUrl) {
          console.log(`✅ Link m3u8 trovato: ${hlsUrl}`);
          return hlsUrl;
        }
      }

      console.log('❌ API non ha restituito un link valido.');
      return null;

    } catch (error) {
      console.error(`❌ Errore API: ${error.message}`);
      return null;
    }
  }

  // ==========================================================
  // METODO 4: Estrai i canali disponibili
  // ==========================================================
  async getAvailableChannels() {
    console.log('📋 Recupero lista canali...');

    try {
      const response = await axios.get(CONFIG.baseUrl, {
        headers: {
          'User-Agent': CONFIG.userAgent
        }
      });

      const $ = cheerio.load(response.data);
      const channels = [];

      // Cerca nei bottoni dei canali
      $('.stream-btn, .card, [onclick*="openLiveTVChannel"]').each((i, el) => {
        const text = $(el).text().trim();
        if (text && text.length > 1) {
          channels.push(text);
        }
      });

      // Cerca anche nei data attributi
      $('[data-channel]').each((i, el) => {
        const name = $(el).attr('data-channel');
        if (name) channels.push(name);
      });

      // Rimuovi duplicati
      const unique = [...new Set(channels)];
      console.log(`✅ Trovati ${unique.length} canali.`);
      
      // Salva su file per riferimento
      fs.writeFileSync('channels.json', JSON.stringify(unique, null, 2));
      console.log('💾 Lista salvata in channels.json');

      return unique;

    } catch (error) {
      console.error(`❌ Errore: ${error.message}`);
      return [];
    }
  }
}

// ============================================================
// ESECUZIONE
// ============================================================
(async () => {
  const extractor = new DamitvExtractor();
  
  // Leggi argomenti da linea di comando
  const args = process.argv.slice(2);
  const command = args[0] || 'help';

  console.log('\n' + '='.repeat(50));
  console.log('  🎬 DAMITV m3u8 Extractor');
  console.log('='.repeat(50) + '\n');

  switch (command) {
    case 'list':
      await extractor.getAvailableChannels();
      break;

    case 'extract':
      const channelName = args[1];
      if (!channelName) {
        console.log('❌ Specifica il nome del canale: node extractor.js extract "CBS Sports"');
        break;
      }
      const url = await extractor.extractWithBrowser(channelName);
      if (url) {
        console.log('\n🎯 URL da usare nel tuo player:');
        console.log(url);
        console.log('\n💾 Copia questo link nel tuo player IPTV (GSE, nPlayer, VLC, ecc.)');
      }
      break;

    case 'api':
      const matchId = args[1];
      if (!matchId) {
        console.log('❌ Specifica l\'ID del match: node extractor.js api "wc/2026-06-16/fra-sen"');
        break;
      }
      const apiUrl = await extractor.extractWithApi(matchId);
      if (apiUrl) {
        console.log('\n🎯 URL da usare nel tuo player:');
        console.log(apiUrl);
      }
      break;

    case 'cbs':
      console.log('📺 Cerco CBS Sports...');
      const cbsUrl = await extractor.extractWithBrowser('CBS Sports');
      if (cbsUrl) {
        console.log('\n🎯 CBS Sports m3u8:');
        console.log(cbsUrl);
      } else {
        console.log('\n❌ CBS Sports non trovato. Prova con:');
        console.log('   node extractor.js list    # per vedere tutti i canali');
        console.log('   node extractor.js extract "NOME_CANALE"');
      }
      break;

    case 'help':
    default:
      console.log(`
📖 COMANDI DISPONIBILI:

  node extractor.js list
    📋 Mostra tutti i canali disponibili

  node extractor.js extract "NOME_CANALE"
    🔍 Estrai il link m3u8 per un canale specifico
    Es: node extractor.js extract "Sky Sports Premier League"

  node extractor.js api "ID_MATCH"
    🔍 Estrai usando l'API interna (per eventi live)
    Es: node extractor.js api "wc/2026-06-16/fra-sen"

  node extractor.js cbs
    🔍 Cerca direttamente CBS Sports

  node extractor.js help
    ❓ Mostra questo menu

📱 COME USARE IL LINK NEL PLAYER IPTV:

  1. Copia il link m3u8 che ottieni
  2. Aprilo in VLC: Media → Apri URL → Incolla
  3. Su iPhone: usa GSE SMART IPTV o nPlayer
  4. Incolla il link come URL di streaming

⚠️ NOTA: I link potrebbero scadere dopo poco tempo
   Puoi ri-eseguire lo script per ottenerne uno nuovo
      `);
      break;
  }

  console.log('\n' + '='.repeat(50) + '\n');
})();

// ============================================================
// ESPORTA PER USO COME MODULO
// ============================================================
module.exports = DamitvExtractor;