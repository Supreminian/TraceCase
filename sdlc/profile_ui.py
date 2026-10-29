"""Local avatar preview; no image leaves the browser before form confirmation."""


def avatar_picker():
    return '''<div class="tc-field tc-avatar-picker">
<label class="tc-label" for="avatar">上传头像</label>
<div class="tc-avatar-upload">
  <img id="avatar-preview" class="tc-avatar-preview" src="/auth/avatar" alt="头像预览">
  <div class="tc-avatar-upload-info">
    <strong id="avatar-preview-title">当前头像</strong>
    <p id="avatar-filename" class="tc-muted">尚未选择新图片</p>
    <input id="avatar" name="avatar" type="file" accept="image/png,image/jpeg,image/webp" hidden>
    <div class="tc-avatar-file-actions">
      <button type="button" class="tc-button secondary" id="avatar-choose">选择图片</button>
      <button type="button" class="tc-button secondary" id="avatar-cancel" hidden>取消更换</button>
    </div>
  </div>
</div>
<small>PNG / JPEG / WebP，最多 2 MiB。预览按居中裁切显示，点击确认保存后才会替换当前头像。</small>
<p id="avatar-status" class="tc-avatar-status" role="status" aria-live="polite">选择图片后可在本机预览，确认前不会上传。</p>
<noscript><style>#avatar[hidden]{display:block!important}#avatar-choose{display:none}</style>选择图片后，点击保存个人资料以提交。</noscript>
</div>'''


AVATAR_PREVIEW_SCRIPT = r'''<script>
(() => {
  const file = document.getElementById('avatar');
  const choose = document.getElementById('avatar-choose');
  const cancel = document.getElementById('avatar-cancel');
  const preview = document.getElementById('avatar-preview');
  const title = document.getElementById('avatar-preview-title');
  const filename = document.getElementById('avatar-filename');
  const status = document.getElementById('avatar-status');
  const save = document.getElementById('profile-save');
  const remove = document.getElementById('remove-avatar');
  if (!file || !save) return;
  let objectURL = null;
  let generation = 0;
  let ready = true;
  const showStatus = (text, error = false) => {
    status.textContent = text;
    status.classList.toggle('error', error);
  };
  const release = () => {
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
  };
  const reset = () => {
    generation += 1;
    release();
    file.value = '';
    preview.src = '/auth/avatar';
    preview.classList.remove('pending');
    title.textContent = '当前头像';
    filename.textContent = '尚未选择新图片';
    cancel.hidden = true;
    save.disabled = false;
    save.textContent = '保存个人资料';
    ready = true;
  };
  choose.addEventListener('click', () => file.click());
  cancel.addEventListener('click', () => {
    reset();
    showStatus('已取消更换，当前头像保持不变。');
  });
  file.addEventListener('change', () => {
    const selected = file.files[0];
    resetPreview(selected);
  });
  function resetPreview(selected) {
    generation += 1;
    const current = generation;
    release();
    if (!selected) {
      reset();
      showStatus('选择图片后可在本机预览，确认前不会上传。');
      return;
    }
    if (remove) remove.checked = false;
    const fail = (message) => {
      reset();
      showStatus(message + ' 当前头像未更改。', true);
    };
    if (!/\.(png|jpe?g|webp)$/i.test(selected.name)
        || (selected.type && !['image/png', 'image/jpeg', 'image/webp'].includes(selected.type))) {
      fail('请选择 PNG、JPEG 或 WebP 图片。');
      return;
    }
    if (selected.size === 0 || selected.size > 2 * 1024 * 1024) {
      fail('图片不能为空，且不能超过 2 MiB。');
      return;
    }
    ready = false;
    save.disabled = true;
    cancel.hidden = false;
    title.textContent = '正在读取图片…';
    filename.textContent = selected.name;
    showStatus('正在准备头像预览…');
    objectURL = URL.createObjectURL(selected);
    const candidate = new Image();
    candidate.onload = () => {
      if (current !== generation) return;
      if (Math.max(candidate.naturalWidth, candidate.naturalHeight) > 4096
          || candidate.naturalWidth * candidate.naturalHeight > 16000000) {
        fail('头像尺寸不能超过 4096 × 4096，且总像素不能超过 1600 万。');
        return;
      }
      preview.src = objectURL;
      preview.classList.add('pending');
      title.textContent = '新头像预览';
      if (remove) remove.checked = false;
      showStatus('尚未保存。确认预览效果后，点击“确认使用新头像并保存”。');
      save.textContent = '确认使用新头像并保存';
      save.disabled = false;
      ready = true;
    };
    candidate.onerror = () => {
      if (current === generation) fail('图片无法预览，请重新选择。');
    };
    candidate.src = objectURL;
  }
  if (remove) remove.addEventListener('change', () => {
    if (remove.checked) {
      reset();
      showStatus('保存后将恢复默认头像，当前头像尚未更改。');
    } else showStatus('当前头像保持不变。');
  });
  file.form.addEventListener('submit', (event) => {
    if (!ready) {
      event.preventDefault();
      showStatus('请等待图片预览完成后再确认保存。');
    }
  });
  window.addEventListener('pagehide', () => { generation += 1; release(); });
})();
</script>'''
