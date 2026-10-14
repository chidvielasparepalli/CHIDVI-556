const input = document.getElementById('pair-code');
const error = document.getElementById('pair-error');
const button = document.getElementById('pair-button');
const token = new URLSearchParams(location.hash.slice(1)).get('pair');
history.replaceState(null, '', '/login');
if (token) input.value = token;
localStorage.removeItem('jarvis_device_token');
sessionStorage.removeItem('jarvis_token');
sessionStorage.removeItem('jarvis_key');
document.getElementById('pair-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (input.value.trim().length !== 43) { error.textContent = 'Scan a new QR code or paste the complete pairing code.'; return; }
  button.disabled = true;
  button.textContent = 'Pairing...';
  error.textContent = '';
  try {
    const response = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:input.value.trim()})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Pairing failed. Request a new code on the laptop.');
    input.value = '';
    location.replace('/');
  } catch (failure) { error.textContent = failure.message || 'Cannot reach the laptop. Check the network connection.'; }
  finally { button.disabled = false; button.textContent = 'Pair with laptop'; }
});
if (!token) fetch('/api/session').then(response => {if(response.ok) location.replace('/');}).catch(() => {});
