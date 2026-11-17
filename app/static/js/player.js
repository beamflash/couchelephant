/* The player, shared by every page that plays something.

   One video element for the whole page, made the first time it is needed, so
   leaving it always stops the stream and gives the tuner back. Home plays
   recordings and channels, Recordings plays recordings, and the guide tunes a
   channel, all through the three calls at the bottom. */
(function () {
  var el = null, video = null, msg = null, hls = null, now = null, beat = null;
  var onNow = null, beat_n = 0, pausedAt = null;
  var breaks = [], breaksTimer = null;
  var CC_KEY = 'ce.captions';

  // Captions on or off is kept in this browser, so the next video starts the
  // way the last one was left. Storage can be missing; then they start off.
  function captionsWanted() {
    try { return localStorage.getItem(CC_KEY) === 'on'; } catch (e) { return false; }
  }
  function keepCaptions(on) {
    try { localStorage.setItem(CC_KEY, on ? 'on' : 'off'); } catch (e) {}
  }
  function captionTracks() {
    return Array.prototype.filter.call(video.textTracks, function (t) {
      return t.kind === 'captions' || t.kind === 'subtitles';
    });
  }
  function showCaptions(on) {
    // Each video adds its own track and the old ones stay on the element, so
    // only the newest is the one with captions in it.
    var all = captionTracks();
    all.forEach(function (t, i) { t.mode = on && i === all.length - 1 ? 'showing' : 'hidden'; });
    var b = el.querySelector('#tvpcc');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.hidden = !captionTracks().length;
  }

  function build() {
    if (el) return;
    el = document.createElement('div');
    el.className = 'tvplayer';
    el.hidden = true;
    el.innerHTML =
      '<div class="tvpbar">' +
        '<img class="tvplogo" id="tvplogo" alt="" hidden>' +
        '<span class="tvpnum" id="tvpnum"></span>' +
        '<div class="tvptitle"><b id="tvpt"></b></div>' +
        '<button type="button" id="tvpinfo" class="tvpinfo" aria-label="About this programme"' +
          ' aria-expanded="false">i</button>' +
        '<button type="button" id="tvpcc" aria-pressed="false" hidden' +
          ' aria-label="Captions">CC</button>' +
        '<button type="button" id="tvpstart">From the start</button>' +
        '<button type="button" id="tvplive" class="tvlivebtn">Live</button>' +
        '<button type="button" id="tvpclose" aria-label="Close the player">Close</button>' +
      '</div>' +
      '<div class="tvpabout" id="tvpabout" hidden>' +
        '<div class="tvpsub dim" id="tvps"></div><p id="tvpdesc"></p>' +
      '</div>' +
      '<video id="tvvideo" controls playsinline></video>' +
      '<div class="tvpmsg" id="tvpmsg" hidden></div>' +
      '<button type="button" class="tvpskip" id="tvpskip" hidden>Skip ad</button>';
    document.body.appendChild(el);
    video = el.querySelector('#tvvideo');
    msg = el.querySelector('#tvpmsg');
    el.querySelector('#tvpclose').addEventListener('click', close);
    el.querySelector('#tvpcc').addEventListener('click', function () {
      var on = this.getAttribute('aria-pressed') !== 'true';
      keepCaptions(on);
      showCaptions(on);
    });
    el.querySelector('#tvpinfo').addEventListener('click', function () {
      var box = el.querySelector('#tvpabout');
      box.hidden = !box.hidden;
      this.setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
      if (!box.hidden) whatIsOn();
    });
    // A track appears once the player has found captions in the video. It is
    // set the way this browser last left captions.
    video.textTracks.addEventListener('addtrack', function () {
      setTimeout(function () { showCaptions(captionsWanted()); }, 0);
    });
    // A second video on the same page reuses the track it already has, which
    // adds nothing, so the setting is laid on again once it plays.
    video.addEventListener('playing', function () { showCaptions(captionsWanted()); });
    el.querySelector('#tvpstart').addEventListener('click', function () {
      video.currentTime = 0; video.play().catch(function () {});
    });
    el.querySelector('#tvplive').addEventListener('click', function () {
      video.currentTime = liveEdge(); video.play().catch(function () {});
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !el.hidden) close();
    });
    window.addEventListener('pagehide', function () {
      // Leaving the page closes the player: live TV gives its tuner back now.
      if (now && now.kind === 'live') navigator.sendBeacon('/api/tv/live/' + now.id + '/stop');
      else report();
    });
    // Inside an ad break the Skip button shows. Nothing skips on its own.
    video.addEventListener('timeupdate', showSkip);
    el.querySelector('#tvpskip').addEventListener('click', function () {
      var b = breakAt(video.currentTime);
      if (b) video.currentTime = b.end;
      showSkip();
    });
    video.addEventListener('pause', function () { pausedAt = Date.now(); });
    video.addEventListener('play', function () { pausedAt = null; });
  }

  function breakAt(t) {
    for (var i = 0; i < breaks.length; i++) {
      // Not in the last second of a break: by then the press would come late.
      if (t >= breaks[i].start && t < breaks[i].end - 1) return breaks[i];
    }
    return null;
  }
  function showSkip() { el.querySelector('#tvpskip').hidden = !breakAt(video.currentTime); }

  // Where the ads are. Asked again every half minute while the recording or
  // the channel is still coming in, because breaks are added as they are found.
  function loadBreaks() {
    if (!now || !now.id) return;
    var asked = now.id;
    fetch('/api/tv/recordings/' + asked + '/breaks').then(CE.readJson).then(function (d) {
      if (!now || now.id !== asked || !d.ok) return;
      breaks = d.breaks || [];
      showSkip();
      if (d.growing) breaksTimer = setTimeout(loadBreaks, 30000);
    }).catch(function () {});
  }

  function say(text) { msg.hidden = !text; msg.textContent = text || ''; }

  // The channel, its logo and the programme on screen. A live channel moves on
  // to the next programme while it plays, so this is asked again now and then.
  function header(channel, title) {
    var logo = el.querySelector('#tvplogo');
    logo.hidden = !channel || !channel.logo;
    if (channel && channel.logo) logo.src = channel.logo;
    el.querySelector('#tvpnum').textContent = channel ? channel.number : '';
    el.querySelector('#tvpt').textContent = title || '';
  }

  function whatIsOn() {
    if (!now || !now.id) return;
    var asked = now.id;
    fetch('/api/tv/recordings/' + asked + '/now?seconds=' + (video.currentTime || 0))
      .then(CE.readJson).then(function (d) {
        if (!d.ok || !now || now.id !== asked) return;
        onNow = d.program;
        header(onNow.channel, onNow.title);
        el.querySelector('#tvps').textContent = onNow.subtitle || '';
        el.querySelector('#tvpdesc').textContent =
          onNow.summary || 'The guide has no description for this programme.';
      }).catch(function () {});
  }

  function liveEdge() {
    if (hls && hls.liveSyncPosition) return hls.liveSyncPosition;
    if (video.seekable.length) return Math.max(0, video.seekable.end(video.seekable.length - 1) - 8);
    return 0;
  }

  function report() {
    if (!now) return;
    // A live session is kept alive; a recording is told where the player is,
    // which is also what holds it back from being moved while it is watched.
    if (now.kind === 'live') {
      fetch('/api/tv/live/' + now.id + '/keepalive', {method: 'POST'});
    } else {
      fetch('/api/tv/recordings/' + now.id + '/position',
            {method: 'POST', body: new URLSearchParams({seconds: video.currentTime || 0})});
    }
  }

  function wait(title, text) {
    build();
    header(null, title);
    el.querySelector('#tvps').textContent = '';
    el.querySelector('#tvpdesc').textContent = '';
    el.querySelector('#tvplive').hidden = true;
    el.querySelector('#tvpstart').hidden = true;
    el.hidden = false;
    say(text);
  }

  /* `at` is 'start', 'resume' or 'live'. */
  function open(r, at) {
    build();
    now = r;
    header(r.channel, r.title);
    el.querySelector('#tvps').textContent = r.subtitle || '';
    el.querySelector('#tvpdesc').textContent = '';
    el.querySelector('#tvpcc').hidden = true;
    el.querySelector('#tvplive').hidden = !!r.complete;
    el.querySelector('#tvpstart').hidden = r.kind === 'live';
    el.hidden = false;
    say('');
    var from = at === 'resume' ? r.position : at === 'live' ? -1 : 0;
    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({startPosition: from, liveDurationInfinity: false});
      hls.on(Hls.Events.ERROR, function (e, data) {
        if (data.fatal) say('The video stopped: ' + data.details + '.');
      });
      hls.loadSource(r.playlist);     // hls.js is told where to start above
      hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari plays it itself, and like the Apple TV it opens a growing
      // playlist at its live end unless the playlist says otherwise.
      video.src = r.playlist + (from === 0 ? '?start=0' : '');
      if (from > 0) {
        video.addEventListener('loadedmetadata', function once() {
          video.removeEventListener('loadedmetadata', once);
          video.currentTime = from;
        });
      }
    } else {
      say('This browser cannot play the video. Use Safari, Chrome, Edge or Firefox.');
      return;
    }
    video.play().catch(function () {});
    report();
    whatIsOn();
    loadBreaks();
    beat = setInterval(function () {
      report();
      // Live TV paused longer than the limit set on the server plays on from
      // where it stopped. The channel is held only that long.
      if (now && now.kind === 'live' && pausedAt && now.pause_limit &&
          Date.now() - pausedAt >= now.pause_limit * 1000) {
        pausedAt = null;
        video.play().catch(function () {});
      }
      if (!(beat_n = (beat_n + 1) % 6)) whatIsOn();   // once a minute
    }, 10000);
  }

  function close() {
    if (now && now.kind === 'live') {
      fetch('/api/tv/live/' + now.id + '/stop', {method: 'POST'});
    } else {
      report();
    }
    clearInterval(beat); beat = null;
    clearTimeout(breaksTimer); breaksTimer = null; breaks = [];
    el.querySelector('#tvpskip').hidden = true;
    if (hls) { hls.destroy(); hls = null; }
    video.pause(); video.removeAttribute('src'); video.load();
    el.hidden = true;
    el.querySelector('#tvpabout').hidden = true;
    el.querySelector('#tvpinfo').setAttribute('aria-expanded', 'false');
    now = null; onNow = null;
    if (CE.Player.onClose) CE.Player.onClose();
  }

  // A recording whose copy was tidied away is made again when it is asked
  // for, and a channel takes a few seconds to tune. Both are waited for here.
  function whenPlayable(id, at, tries) {
    fetch('/api/tv/recordings/' + id).then(CE.readJson).then(function (d) {
      if (!d.ok) { say(d.error); return; }
      if (d.recording.playable && d.recording.seconds > 0) { open(d.recording, at); return; }
      if (tries <= 0) { say('The video did not start. Try again in a moment.'); return; }
      setTimeout(function () { whenPlayable(id, at, tries - 1); }, 1500);
    });
  }

  CE.Player = {
    /* Play a recording the page already holds. */
    play: function (r, at) {
      if (r.seconds > 0) { open(r, at); return; }
      wait(r.title, 'Getting the video ready...');
      whenPlayable(r.id, at, 40);
    },
    /* Watch a channel live. */
    watch: function (vcn) {
      wait('Channel ' + vcn, 'Tuning...');
      fetch('/api/tv/live/' + encodeURIComponent(vcn), {method: 'POST'})
        .then(CE.readJson).then(function (d) {
          if (!d.ok) { say(d.error); return; }
          now = d.recording;       // so Close gives the tuner back even now
          whenPlayable(d.recording.id, 'live', 40);
        });
    },
    isOpen: function () { return !!el && !el.hidden; },
    onClose: null
  };
})();
