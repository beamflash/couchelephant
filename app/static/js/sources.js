/* The three collapsible sections under "Recorded by" in every season pass
   panel: Plex settings, Filter networks and Filter channels.

   Each one is a native <details>, closed until the person opens it, with a
   short summary on its header. The guide's record panel and the pass editor
   both use this one component, so the panels cannot drift apart again.

   state.nets and state.chans are the arrays this edits in place. onChange
   runs after every tick so the caller can repaint its verdict bar. */
(function (w) {
  'use strict';
  var esc = w.CE.esc;

  function SourcePicker(state, onChange) {
    this.state = state;
    this.onChange = onChange || function () {};
    this.cache = null;
    // Which sections are open, kept here because the panels redraw.
    this.open = {plex: false, nets: false, chans: false};
    this.q = {net: '', ch: ''};
    this.nsettings = 0;
  }

  function pickedText(list) {
    return !list.length ? 'Any'
      : list.length <= 2 ? list.join(', ')
      : list.length + ' chosen';
  }

  function section(key, title, summary, body, na, why, extra) {
    return '<details class="optsec' + (na ? ' na' : '') + '" id="sec-' + key +
      '" data-sec="' + key + '"' + (!na && extra.open ? ' open' : '') + '>' +
      '<summary' + (why ? ' title="' + esc(why) + '"' : '') + '>' +
      '<span class="secname">' + esc(title) + '</span>' +
      '<span class="secsum" id="' + (key === 'nets' ? 'net' : key === 'chans' ? 'ch' : key) + 'sum">' + esc(summary) + '</span>' +
      '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" ' +
        'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" ' +
        'stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></summary>' +
      '<div class="secbody">' + body + '</div></details>';
  }

  function pickBody(kind, note) {
    return '<div class="note">' + esc(note) + '</div>' +
      '<input class="multiq" data-q="' + kind + '" placeholder="Search..." ' +
        'autocomplete="off" value="">' +
      '<div class="multibody" id="' + kind + 'body"></div>';
  }

  /* The sections, ready to drop under the Recorded by row. `settings` are
     Plex's own rows; `na`/`why` turn the two filters off with a reason (a
     single broadcast is already one channel at one time). */
  SourcePicker.prototype.sections = function (settings, na, why) {
    var h = '', self = this, st = this.state;
    settings = settings || [];
    this.nsettings = settings.length;
    if (settings.length) {
      h += section('plex', 'Plex settings',
        settings.length + (settings.length === 1 ? ' setting' : ' settings'),
        '<div class="optgrid">' + settings.map(w.CE.settingField).join('') + '</div>',
        false, '', {open: this.open.plex});
    }
    var hint = 'Plex takes one channel. This takes as many as you like.';
    h += section('nets', 'Filter networks', na ? 'Not needed' : pickedText(st.nets),
      pickBody('net', hint), na, na ? why : hint, {open: this.open.nets});
    h += section('chans', 'Filter channels', na ? 'Not needed' : pickedText(st.chans),
      pickBody('ch', hint), na, na ? why : hint, {open: this.open.chans});
    this.na = !!na;
    return '<div class="optsecs">' + h + '</div>';
  };

  SourcePicker.prototype.paint = function () {
    var st = this.state;
    function set(id, text, on) {
      var el = document.getElementById(id);
      if (el) { el.textContent = text; el.classList.toggle('set', on); }
    }
    if (this.na) return;
    set('netsum', pickedText(st.nets), !!st.nets.length);
    set('chsum', pickedText(st.chans), !!st.chans.length);
  };

  SourcePicker.prototype.wire = function () {
    var self = this;
    document.querySelectorAll('details.optsec').forEach(function (d) {
      var key = d.dataset.sec;
      d.querySelector('summary').addEventListener('click', function (e) {
        if (d.classList.contains('na')) e.preventDefault();
      });
      d.addEventListener('toggle', function () { self.open[key] = d.open; });
    });
    document.querySelectorAll('input.multiq').forEach(function (inp) {
      inp.addEventListener('input', function () {
        self.q[inp.dataset.q] = inp.value;
        self.render();
      });
    });
    if (this.na) return;
    this.load().then(function () { self.render(); self.paint(); });
  };

  SourcePicker.prototype.load = function () {
    var self = this;
    if (this.cache) return Promise.resolve(this.cache);
    return fetch('/api/sources').then(function (r) { return r.json(); })
      .then(function (d) { self.cache = d; return d; });
  };

  SourcePicker.prototype.render = function () {
    if (!this.cache) return;
    var st = this.state, self = this;

    function item(kind, value, label, note, on) {
      return '<label class="multirow"><input type="checkbox" data-' + kind + '="' +
        esc(value) + '"' + (on ? ' checked' : '') + '><span class="mt">' + esc(label) +
        '</span>' + (note ? '<span class="mn">' + esc(note) + '</span>' : '') + '</label>';
    }
    function fill(id, h) {
      var el = document.getElementById(id);
      if (el) el.innerHTML = h || '<span class="multinone">Nothing matches.</span>';
      return el;
    }

    var qn = this.q.net.toLowerCase(), qc = this.q.ch.toLowerCase(), h = '';
    this.cache.networks.filter(function (n) {
      return !qn || n.name.toLowerCase().indexOf(qn) !== -1;
    }).forEach(function (n) {
      h += item('net', n.name, n.name, n.channels.join(', '), st.nets.indexOf(n.name) !== -1);
    });
    var nb = fill('netbody', h);
    h = '';
    this.cache.channels.filter(function (c) {
      return !qc || (c.vcn + ' ' + c.call_sign + ' ' + c.network)
        .toLowerCase().indexOf(qc) !== -1;
    }).forEach(function (c) {
      h += item('ch', c.vcn, c.vcn + ' ' + c.call_sign, c.network,
                st.chans.indexOf(c.vcn) !== -1);
    });
    var cb = fill('chbody', h);

    function flip(list, v) {
      var i = list.indexOf(v);
      if (i === -1) list.push(v); else list.splice(i, 1);
      self.paint();
      self.onChange();
    }
    [[nb, '[data-net]', 'net', st.nets], [cb, '[data-ch]', 'ch', st.chans]]
      .forEach(function (g) {
        if (!g[0]) return;
        g[0].querySelectorAll(g[1]).forEach(function (box) {
          box.addEventListener('change', function () { flip(g[3], box.dataset[g[2]]); });
        });
      });
  };

  w.CE.SourcePicker = SourcePicker;
})(window);
