// Sign in with Plex. Works on any block marked [data-plex-signin]: the welcome
// page and Settings share it. The account token never reaches the page; it
// only sees the code to type at plex.tv/link and the names of the servers.
(function () {
  function init(box) {
    if (box.getAttribute('data-wired')) return;
    box.setAttribute('data-wired', '1');
    var go = box.querySelector('.psi-go');
    var out = box.querySelector('.psi-out');
    var timer = null;
    var dead = false;                               // the overlay closed on us
    var ctls = [];
    var errors = 0;
    var list = document.createElement('div');       // server buttons, kept across a failed pick
    list.className = 'psi-list';
    out.parentNode.insertBefore(list, out.nextSibling);
    var landing = box.getAttribute('data-after');   // 'guide' or 'reload'

    // Every call has its own clock, so a stuck request ends in a plain message.
    // The limits sit above the server's worst case (about 20 s).
    function api(url, opts, ms) {
      var ctl = new AbortController();
      ctls.push(ctl);
      var t = setTimeout(function () { ctl.abort(); }, ms || 15000);
      opts = opts || {};
      opts.signal = ctl.signal;
      return fetch(url, opts).then(function (r) { clearTimeout(t); return r.json(); },
                                   function (e) { clearTimeout(t); throw e; });
    }

    // Called when the Settings overlay closes. Nothing after this may touch
    // the page: no poll, no pick, no reload.
    box._psiStop = function () {
      dead = true;
      clearTimeout(timer);
      ctls.forEach(function (c) { c.abort(); });
    };
    function gone() { return dead || !box.isConnected; }

    function oops(e) {
      if (gone()) return;
      say('bad', e && e.name === 'AbortError'
        ? 'That took too long. Try again.'
        : 'Could not reach CouchElephant itself. Try again.');
      reset();
    }

    function say(kind, text) {
      out.hidden = false;
      out.innerHTML = '';
      var d = document.createElement('div');
      d.className = 'verdict ' + kind;
      var m = document.createElement('span'); m.className = 'vmark'; m.setAttribute('aria-hidden', 'true');
      var t = document.createElement('span'); t.className = 'vtext'; t.textContent = text;
      d.appendChild(m); d.appendChild(t);
      out.appendChild(d);
      return d;
    }

    function zone() {
      var sel = box.getAttribute('data-zone') && document.querySelector(box.getAttribute('data-zone'));
      return sel ? sel.value : '';
    }

    function lang() {
      var sel = box.getAttribute('data-language') && document.querySelector(box.getAttribute('data-language'));
      return sel ? sel.value : '';
    }

    function reset() {
      go.disabled = false;
      go.textContent = 'Sign in with Plex';
    }

    function done(j) {
      // The welcome page shows the QR to connect the app instead of leaving.
      if (landing === 'guide' && window.ceShowConnect) {
        say('ok', j.detail);
        return window.ceShowConnect();
      }
      say('ok', j.detail + (landing === 'guide' ? ' Taking you to the guide...' : ' Saved.'));
      setTimeout(function () {
        if (landing === 'guide') location.href = '/'; else location.reload();
      }, 1300);
    }

    function use(pin, id) {
      say('busy', 'Checking the server...');
      var btns = list.querySelectorAll('button');
      btns.forEach(function (b) { b.disabled = true; });
      var body = new URLSearchParams({server: id, timezone: zone(), preferred_language: lang()});
      api('/settings/plex/signin/' + pin + '/use', {method: 'POST', body: body}, 30000)
        .then(function (j) {
          if (gone()) return;
          if (j.ok) return done(j);
          btns.forEach(function (b) { b.disabled = false; });
          var d = say('bad', j.detail);
          if (j.tried && j.tried.length) {
            var p = document.createElement('div');
            p.className = 'help';
            p.textContent = 'Tried: ' + j.tried.join(', ');
            d.appendChild(p);
          }
          reset();
        })
        .catch(oops);
    }

    function pick(pin, servers, detail) {
      list.innerHTML = '';
      if (servers.length === 1) return use(pin, servers[0].id);
      out.hidden = false;
      out.innerHTML = '';
      var h = document.createElement('div');
      h.className = 'help';
      h.textContent = servers.length ? 'Pick your server.'
        : (detail || 'Plex shows no servers on this account.');
      out.appendChild(h);
      servers.forEach(function (s) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'psi-server';
        b.textContent = s.name;
        b.addEventListener('click', function () { use(pin, s.id); });
        list.appendChild(b);
      });
      if (!servers.length) reset();
    }

    function poll(pin) {
      if (gone()) return;
      api('/settings/plex/signin/' + pin, {headers: {Accept: 'application/json'}}, 25000)
        .then(function (j) {
          if (gone()) return;
          if (j.state === 'waiting' || (j.state === 'error' && ++errors < 5)) {
            var w = out.querySelector('.psi-wait');
            if (j.state === 'waiting') errors = 0;
            if (w) w.textContent = j.state === 'waiting' ? 'Waiting for you to approve it...'
              : 'plex.tv did not answer, still trying.';
            timer = setTimeout(function () { poll(pin); }, 2000);
            return;
          }
          if (j.state === 'servers') return pick(pin, j.servers, j.detail);
          say('bad', j.state === 'expired' ? 'That code has expired. Get a new one.'
                                           : (j.detail || 'Something went wrong.'));
          reset();
        })
        .catch(oops);
    }

    go.addEventListener('click', function () {
      clearTimeout(timer);
      errors = 0;
      list.innerHTML = '';
      go.disabled = true;
      go.textContent = 'Waiting for Plex...';
      say('busy', 'Asking plex.tv for a code...');
      api('/settings/plex/signin', {method: 'POST', headers: {Accept: 'application/json'}})
        .then(function (j) {
          if (!j.ok) { say('bad', j.detail || 'Could not start the sign-in.'); reset(); return; }
          out.hidden = false;
          out.innerHTML = '';
          var p = document.createElement('p');
          p.append('Open ');
          var a = document.createElement('a');
          a.href = j.link; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'plex.tv/link';
          p.appendChild(a);
          p.append(' and type this code:');
          var c = document.createElement('div');
          c.className = 'psi-code'; c.textContent = j.code;
          out.appendChild(p); out.appendChild(c);
          var w = document.createElement('div');
          w.className = 'help psi-wait'; w.textContent = 'Waiting for you to approve it...';
          out.appendChild(w);
          poll(j.pin);
        })
        .catch(oops);
    });
  }

  // The overlay is closing: stop every sign-in inside it.
  window.stopPlexSignin = function (root) {
    (root || document).querySelectorAll('[data-plex-signin]').forEach(function (b) {
      if (b._psiStop) b._psiStop();
    });
  };

  // Settings is also loaded as a fragment, so it can wire itself later.
  window.wirePlexSignin = function (root) {
    (root || document).querySelectorAll('[data-plex-signin]').forEach(init);
  };
  window.wirePlexSignin(document);
})();
