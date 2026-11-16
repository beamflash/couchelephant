/* The player, shared by every page that plays something.

   One video element for the whole page, made the first time it is needed, so
   leaving it always stops the stream and gives the tuner back. Home plays
   recordings and channels, Recordings plays recordings, and the guide tunes a
   channel, all through the three calls at the bottom. */
(function () {
  var el = null, video = null, msg = null, hls = null, now = null, beat = null;

  function build() {
    if (el) return;
    el = document.createElement('div');
    el.className = 'tvplayer';
    el.hidden = true;
    el.innerHTML =
      '<div class="tvpbar">' +
        '<div class="tvptitle"><b id="tvpt"></b><span class="dim" id="tvps"></span></div>' +
        '<button type="button" id="tvpstart">From the start</button>' +
        '<button type="button" id="tvplive" class="tvlivebtn">Live</button>' +
        '<button type="button" id="tvpclose" aria-label="Close the player">Close</button>' +
      '</div>' +
      '<video id="tvvideo" controls playsinline></video>' +
      '<div class="tvpmsg" id="tvpmsg" hidden></div>';
    document.body.appendChild(el);
    video = el.querySelector('#tvvideo');
    msg = el.querySelector('#tvpmsg');
    el.querySelector('#tvpclose').addEventListener('click', close);
    el.querySelector('#tvpstart').addEventListener('click', function () {
      video.currentTime = 0; video.play().catch(function () {});
    });
    el.querySelector('#tvplive').addEventListener('click', function () {
      video.currentTime = liveEdge(); video.play().catch(function () {});
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !el.hidden) close();
    });
    window.addEventListener('pagehide', report);
  }

  function say(text) { msg.hidden = !text; msg.textContent = text || ''; }

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
    el.querySelector('#tvpt').textContent = title;
    el.querySelector('#tvps').textContent = '';
    el.querySelector('#tvplive').hidden = true;
    el.querySelector('#tvpstart').hidden = true;
    el.hidden = false;
    say(text);
  }

  /* `at` is 'start', 'resume' or 'live'. */
  function open(r, at) {
    build();
    now = r;
    el.querySelector('#tvpt').textContent = r.title;
    el.querySelector('#tvps').textContent = r.subtitle ? ' ' + r.subtitle : '';
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
    beat = setInterval(report, 10000);
  }

  function close() {
    report();
    clearInterval(beat); beat = null;
    if (hls) { hls.destroy(); hls = null; }
    video.pause(); video.removeAttribute('src'); video.load();
    el.hidden = true;
    now = null;
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
