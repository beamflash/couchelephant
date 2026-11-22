// Reports: the "Report this problem" button, the "Report an issue" row, and the
// dialog both open. One click handler on the document serves every
// [data-report] and [data-report-open], so HTML built later needs no wiring.
//
// Places that deliberately have no button, because the thing shown is an
// expected state and not a fault (spec call 7):
//   welcome.html: "no tuner found yet" and the other setup waiting states
//   settings: the tuner scan results (none found, found, scanning)
//   anything that says "not signed in" or "no recordings yet"
(function () {
  var CONSENT_KEY = 'ce.report.consent', EMAIL_KEY = 'ce.report.email';

  // ---- what this browser has complained about, kept for the report ----
  var LOG = [];
  function keep(line) {
    LOG.push(new Date().toISOString() + ' ' + String(line).slice(0, 1000));
    if (LOG.length > 100) LOG.shift();
  }
  var prevOnError = window.onerror;
  window.onerror = function (msg, src, line, col) {
    keep('error: ' + msg + ' (' + (src || '') + ':' + (line || 0) + ':' + (col || 0) + ')');
    return prevOnError ? prevOnError.apply(this, arguments) : false;
  };
  window.addEventListener('unhandledrejection', function (e) {
    keep('unhandled rejection: ' + ((e.reason && e.reason.message) || e.reason));
  });
  var realError = console.error;
  console.error = function () {
    try {
      keep('console.error: ' + Array.prototype.map.call(arguments, function (a) {
        return typeof a === 'string' ? a : (a && a.message) || JSON.stringify(a);
      }).join(' '));
    } catch (e) { /* a log line is never worth a second error */ }
    return realError.apply(console, arguments);
  };

  // localStorage can throw (private mode, blocked site data), so every use is wrapped.
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* not remembered */ } }

  function attr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/'/g, '&#39;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function button(trigger) {
    var t = {error: String((trigger && trigger.error) || '').slice(0, 2000),
             screen: String((trigger && trigger.screen) || location.pathname)};
    return '<button type="button" class="reportbtn tiny" data-report=\'' +
      attr(JSON.stringify(t)) + '\'>Report this problem</button>';
  }

  var dlg, box, trig, busy;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function close() { if (dlg) dlg.classList.remove('show'); busy = false; }

  function shell(title) {
    box.textContent = '';
    var h = el('h2', 'reporttitle', title);
    h.id = 'reporttitle';
    box.appendChild(h);
    return h;
  }

  function buttons(sendLabel, onSend) {
    var row = el('div', 'reportfoot');
    var send = el('button', 'primary', sendLabel);
    send.type = 'button'; send.id = 'reportsend';
    var no = el('button', '', 'Never mind');
    no.type = 'button'; no.id = 'reportno';
    no.addEventListener('click', close);
    if (onSend) send.addEventListener('click', function () { onSend(send); });
    row.appendChild(send); row.appendChild(no);
    return row;
  }

  function say(text, cls) {
    var m = document.getElementById('reportmsg');
    if (!m) return;
    m.textContent = text; m.className = 'reportmsg ' + (cls || '');
  }

  function summary() {
    if (trig.error) {
      return 'Problem: ' + trig.error.slice(0, 300) +
        (trig.screen ? ' (on ' + trig.screen + ')' : '');
    }
    return 'This goes with recent errors from this browser and the state of this CouchElephant.';
  }

  // The first time, the agreement sits at the top of the same form; Send is
  // the agreement. After that it is just the form.
  function formScreen(first) {
    shell(first ? 'Send a report?' : 'Report an issue');
    if (first) {
      var c = el('div', 'reportconsent'); c.id = 'reportconsent';
      c.appendChild(el('p', '',
        'A report holds what you type here, your email if you give one, the problem ' +
        'you were looking at, recent errors from this browser, and the state of this ' +
        'CouchElephant: its version, settings, schedule and recent log. Names, ' +
        'addresses and passwords are removed first.'));
      c.appendChild(el('p', '',
        'Only the person who makes CouchElephant can read it. It is sealed before it ' +
        'leaves, and stored sealed.'));
      box.appendChild(c);
    }
    box.appendChild(el('p', 'reportsum', summary()));
    var f1 = el('label', 'reportfield', 'Message (optional)');
    var msg = el('textarea'); msg.id = 'reportmessage'; msg.rows = 5; msg.maxLength = 20000;
    msg.placeholder = 'What happened, or what did you expect?';
    f1.appendChild(msg);
    var f2 = el('label', 'reportfield', 'Your email (so you can be answered)');
    var em = el('input'); em.id = 'reportemail'; em.type = 'email'; em.autocomplete = 'email';
    em.maxLength = 254;
    f2.appendChild(em);
    box.appendChild(f1); box.appendChild(f2);
    var m = el('div', 'reportmsg', ''); m.id = 'reportmsg';
    box.appendChild(m);
    box.appendChild(buttons('Send', function (b) { send(b, msg, em, first); }));

    var saved = lsGet(EMAIL_KEY);
    if (saved) em.value = saved;
    else {
      // Admin-only on the server: anything but a 200 just leaves the field empty.
      fetch('/api/report/email').then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j && j.email && !em.value) em.value = j.email; })
        .catch(function () {});
    }
    msg.focus();
  }

  function post(path, body) {
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body)})
      .then(function (r) {
        return r.json().then(function (j) { return [r.status, j]; },
                             function () { return [r.status, null]; });
      });
  }

  // Records the agreement on the server. Resolves true, or says why in the dialog.
  function agree() {
    return fetch('/api/report/consent', {method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'agree=1'})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || 'refused');
        lsSet(CONSENT_KEY, '1');
      });
  }

  function send(b, msg, em, first) {
    if (busy) return;
    busy = true; b.disabled = true; say('Sending...', '');
    if (first) {
      agree().then(function () { send2(b, msg, em); }, function (e) {
        busy = false; b.disabled = false;
        say('Could not save your choice: ' + e.message + '. Nothing was sent.', 'bad');
      });
      return;
    }
    send2(b, msg, em);
  }

  function send2(b, msg, em) {
    var email = em.value.trim();
    var body = {
      trigger: trig.error ? {kind: trig.kind || 'error', error: trig.error, screen: trig.screen}
                          : {kind: trig.kind || 'support', screen: trig.screen},
      message: msg.value, email: email,
      client: {ua: navigator.userAgent, screen_size: innerWidth + 'x' + innerHeight},
      app_log: LOG.slice(-100)
    };
    post('/api/report/send', body)
      .then(function (r) {
        // This browser agreed but this server has not heard it (another server,
        // or its settings were reset). Say it again, once, then retry.
        if (r[0] === 403 && r[1] && r[1].error === 'consent') {
          return fetch('/api/report/consent', {method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'agree=1'})
            .then(function () { return post('/api/report/send', body); });
        }
        return r;
      })
      .then(function (r) {
        busy = false;
        var j = r[1];
        if (!j) {
          b.disabled = false;
          say('CouchElephant answered with an error, so nothing was sent.', 'bad');
          return;
        }
        if (!j.ok) {
          b.disabled = false;
          say(j.error || 'The report could not be sent.', 'bad');
          return;
        }
        if (email) lsSet(EMAIL_KEY, email);
        done(j);
      })
      .catch(function (e) {
        busy = false; b.disabled = false;
        say('Could not reach CouchElephant, so nothing was sent. Try again in a moment.', 'bad');
      });
  }

  function done(j) {
    shell(j.sent ? 'Sent' : 'Saved');
    var p = el('p', 'reportmsg ' + (j.sent ? 'ok' : 'warn'), j.message || 'Sent. Thank you.');
    p.id = 'reportresult';
    box.appendChild(p);
    if (j.reportId) {
      var id = el('p', 'dim tiny', 'Report id: ' + j.reportId);
      id.id = 'reportid';
      box.appendChild(id);
    }
    var row = el('div', 'reportfoot');
    var c = el('button', 'primary', 'Close'); c.type = 'button'; c.id = 'reportclose';
    c.addEventListener('click', close);
    row.appendChild(c); box.appendChild(row);
    c.focus();
  }

  function focusFirst() {
    var b = document.getElementById('reportsend');
    if (b) b.focus();
  }

  function open(trigger) {
    dlg = document.getElementById('reportdlg'); box = document.getElementById('reportbox');
    if (!dlg || !box) return;
    trig = {error: '', screen: location.pathname, kind: 'support'};
    if (trigger && trigger.error) {
      trig.error = String(trigger.error).slice(0, 2000);
      trig.kind = trigger.kind || 'error';
    }
    if (trigger && trigger.screen) trig.screen = String(trigger.screen);
    busy = false;
    dlg.classList.add('show');
    // Consent is per person: the server may already have heard yes from someone
    // else, but this browser has not.
    formScreen(lsGet(CONSENT_KEY) !== '1');
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-report],[data-report-open]');
    if (t) {
      // Capture phase: a button inside a clickable row must not also open the row.
      e.stopPropagation(); e.preventDefault();
      var trg = null;
      if (t.hasAttribute('data-report')) {
        try { trg = JSON.parse(t.getAttribute('data-report')); } catch (err) { trg = null; }
      }
      open(trg);
      return;
    }
    if (dlg && e.target === dlg && !busy) close();
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && dlg && dlg.classList.contains('show')) {
      e.stopPropagation(); if (!busy) close();
    }
  }, true);

  window.CEReport = {button: button, open: open};
})();
