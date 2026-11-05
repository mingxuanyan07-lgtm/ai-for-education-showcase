// Read a DSH session log (concatenated Zstandard frames, JSONL) and write UTF-8 to stdout.
// Node's zstdDecompressSync decodes only the first frame, so frames are split by
// walking the Zstandard frame format before decompressing each one.
const fs = require('node:fs');
const zlib = require('node:zlib');

const ZSTD_MAGIC = 0xfd2fb528;

function splitFrames(input) {
  const frames = [];
  let pos = 0;
  while (pos < input.length) {
    const start = pos;
    if (pos + 4 > input.length || input.readUInt32LE(pos) !== ZSTD_MAGIC) {
      throw new Error(`not a Zstandard frame at byte ${pos}`);
    }
    pos += 4;
    const descriptor = input[pos];
    pos += 1;
    const fcsFlag = (descriptor >> 6) & 3;
    const singleSegment = (descriptor >> 5) & 1;
    const checksum = (descriptor >> 2) & 1;
    const dictFlag = descriptor & 3;
    if (!singleSegment) pos += 1; // Window_Descriptor
    pos += [0, 1, 2, 4][dictFlag]; // Dictionary_ID
    const contentSizeBytes = fcsFlag === 0 ? (singleSegment ? 1 : 0)
      : fcsFlag === 1 ? 2 : fcsFlag === 2 ? 4 : 8;
    pos += contentSizeBytes;
    for (;;) {
      if (pos + 3 > input.length) throw new Error('truncated block header');
      const header = input[pos] | (input[pos + 1] << 8) | (input[pos + 2] << 16);
      pos += 3;
      const lastBlock = header & 1;
      const blockType = (header >> 1) & 3;
      const blockSize = header >> 3;
      if (blockType === 0) pos += blockSize;
      else if (blockType === 1) pos += 1;
      else if (blockType === 2) pos += blockSize;
      else throw new Error('reserved Zstandard block type');
      if (lastBlock) break;
    }
    if (checksum) pos += 4;
    if (pos > input.length) throw new Error('truncated Zstandard frame');
    frames.push(input.subarray(start, pos));
  }
  return frames;
}

try {
  const input = fs.readFileSync(process.argv[2]);
  const output = splitFrames(input).map((frame) => zlib.zstdDecompressSync(frame));
  process.stdout.write(Buffer.concat(output));
} catch (error) {
  process.stderr.write(String(error && error.message ? error.message : error));
  process.exitCode = 1;
}
