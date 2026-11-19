// "Update available" banner. The server only tells administrators, so a
// missing answer just means nothing is drawn.
(function () {
  function show(d) {
    var bar = document.getElementById('updatebar');
    if (!bar || !d || !d.latest) return;
    bar.textContent = '';
    var text = document.createElement('span');
    text.className = 'ub-text';
    text.textContent = 'Update available: ' + d.latest;
    bar.appendChild(text);
    var note = document.createElement('span');
    note.className = 'ub-note';
    if (d.click) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.id = 'updatebtn';
      btn.textContent = 'Update now';
      btn.addEventListener('click', function () {
        btn.disabled = true;
        note.textContent = 'Downloading the update...';
        fetch('/api/update/apply', {method: 'POST'})
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j.ok) { note.textContent = j.message; return; }
            btn.disabled = false;
            note.textContent = j.error || 'The update failed.';
            addLink(bar, d.url);
          })
          .catch(function () { note.textContent = 'CouchElephant is restarting.'; });
      });
      bar.appendChild(btn);
    } else {
      addLink(bar, d.url);
      if (d.how === 'docker') note.textContent = 'Pull the new image and recreate the container.';
      if (d.how === 'command') note.textContent = 'curl -fsSL https://couchelephant.app/install.sh | sudo sh';
    }
    bar.appendChild(note);
    bar.hidden = false;
  }
  function addLink(bar, url) {
    if (!url || bar.querySelector('a')) return;
    var a = document.createElement('a');
    a.href = url;
    a.textContent = 'Release page';
    a.target = '_blank';
    a.rel = 'noopener';
    bar.insertBefore(a, bar.children[1] || null);
  }
  fetch('/api/update').then(function (r) { return r.ok ? r.json() : null; })
    .then(show).catch(function () {});
})();
