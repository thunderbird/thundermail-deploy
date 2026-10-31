import crypto from 'crypto';

const POLICY_CLOSE = Buffer.from([0x88, 0x82, 0, 0, 0, 0, 0x03, 0xf0]);
const MAX_MESSAGE = 1048576;
const MAX_FRAGMENTS = 1024;

function clientKey(r) {
  const auth = r.headersIn.Authorization;
  return r.remoteAddress + (auth ? ' ' + crypto.createHash('sha256').update(auth).digest('base64url') : '');
}

function filter(r) {
  if (r.method === 'POST' && !isUserMessage(r.requestText)) {
    r.return(403);
    return;
  }

  r.internalRedirect('@stalwart');
}

function isUserMessage(text) {
  try {
    const calls = JSON.parse(text).methodCalls;
    return calls === undefined || (Array.isArray(calls) && calls.every((call) =>
      Array.isArray(call) && typeof call[0] === 'string' && !call[0].startsWith('x:')));
  } catch (e) {
    return false;
  }
}

function wsFilter(s) {
  let pending = Buffer.alloc(0);
  let upgraded = false;
  let blocked = false;
  let frames = [];
  let payload = [];
  let size = 0;

  function block(reason) {
    blocked = true;
    s.error(`blocked JMAP WebSocket session from ${s.remoteAddress}: ${reason}`);
    s.send(POLICY_CLOSE);
  }

  s.on('upstream', (data) => {
    if (blocked) {
      return;
    }

    pending = Buffer.concat([pending, data]);

    if (!upgraded) {
      const end = pending.indexOf('\r\n\r\n');
      if (end < 0) {
        return;
      }
      s.send(pending.subarray(0, end + 4));
      pending = pending.subarray(end + 4);
      upgraded = true;
    }

    while (pending.length >= 2) {
      const first = pending[0];
      const opcode = first & 0x0f;
      const control = opcode >= 8;
      let length = pending[1] & 0x7f;
      let offset = 2;

      if (length === 126) {
        if (pending.length < 4) {
          return;
        }
        length = pending.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (pending.length < 10) {
          return;
        }
        length = pending.readUInt32BE(2) * 0x100000000 + pending.readUInt32BE(6);
        offset = 10;
      }

      const valid = (first & 0x70) === 0 && (pending[1] & 0x80) !== 0 && (control
        ? opcode <= 10 && length <= 125 && (first & 0x80) !== 0
        : opcode <= 2 && (opcode === 0) === (frames.length > 0) && size + length <= MAX_MESSAGE
          && frames.length < MAX_FRAGMENTS);
      if (!valid) {
        block('frame rule');
        return;
      }

      offset += 4;
      if (pending.length < offset + length) {
        return;
      }

      const frame = pending.subarray(0, offset + length);
      pending = pending.subarray(offset + length);

      if (control) {
        s.send(frame);
        continue;
      }

      const unmasked = Buffer.from(frame.subarray(offset, offset + length));
      const key = frame.readUInt32LE(offset - 4);
      if (key !== 0) {
        const whole = length >>> 2;
        if (unmasked.byteOffset % 4 === 0) {
          const words = new Uint32Array(unmasked.buffer, unmasked.byteOffset, whole);
          for (let i = 0; i < whole; i++) {
            words[i] ^= key;
          }
        } else {
          for (let i = 0; i < whole * 4; i++) {
            unmasked[i] ^= frame[offset - 4 + (i % 4)];
          }
        }
        for (let i = whole * 4; i < length; i++) {
          unmasked[i] ^= frame[offset - 4 + (i % 4)];
        }
      }
      frames.push(frame);
      payload.push(unmasked);
      size += length;

      if (first & 0x80) {
        if (!isUserMessage(Buffer.concat(payload).toString())) {
          block('admin method or invalid JSON');
          return;
        }
        frames.forEach((f) => s.send(f));
        frames = [];
        payload = [];
        size = 0;
      }
    }
  });
}

export default { clientKey, filter, wsFilter };
