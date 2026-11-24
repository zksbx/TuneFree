/**
 * TuneFree - QQ音乐 mflac / mgg 解密钩子
 *
 * 由 main.py 通过 Frida 注入到 QQMusic.exe，对外暴露 rpc.exports。
 *
 * 与旧版的差别：
 * 1. 所有导出符号都做存在性校验，报错能看懂；
 * 2. 解密缓冲不再用 Memory.alloc + free（堆不匹配会静默失效，地址空间只增不减，
 *    32 位 QQ音乐转一百来首 SQ 就会耗尽 4GB），改成 VirtualAlloc / VirtualFree；
 * 3. 解密改成「读 4MB → 写 4MB」流式，峰值占用跟单曲大小无关；
 * 4. 另开 probealloc()，让 Python 侧在批量转换中途探测地址空间余量。
 */
const TARGET_DLL = "QQMusicCommon.dll";

/* MSVC 修饰名。QQ音乐换编译器 / 改类定义都会变，这里是唯一需要跟着更新的地方。 */
var SYMBOLS = {
  ctor: "??0EncAndDesMediaFile@@QAE@XZ",
  dtor: "??1EncAndDesMediaFile@@QAE@XZ",
  open: "?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z",
  getSize: "?GetSize@EncAndDesMediaFile@@QAEKXZ",
  read: "?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z",
};

var BOUND = null;
var OBJECT_SIZE = 0x28;          // EncAndDesMediaFile 的实例大小，按当前版本实测
var MAX_FILE_SIZE = 512 * 1024 * 1024;
var READ_CHUNK = 4 * 1024 * 1024;  // 单次 Read / 单块缓冲的上限

/** 把 5 个符号全部找出来并绑定成 NativeFunction，只在第一次调用时做。 */
function bindSymbols() {
  if (BOUND !== null) {
    return BOUND;
  }

  var missing = [];
  var addr = {};
  for (var key in SYMBOLS) {
    var a = Module.findExportByName(TARGET_DLL, SYMBOLS[key]);
    if (a === null) {
      missing.push(SYMBOLS[key]);
    }
    addr[key] = a;
  }

  if (missing.length > 0) {
    throw new Error(
      "QQ音乐版本不匹配：在 " +
        TARGET_DLL +
        " 里找不到 " +
        missing.length +
        " 个导出符号（" +
        missing.join("、") +
        "）。请把 QQ音乐降级到 TuneFree 适配的版本，或等 TuneFree 更新。"
    );
  }

  BOUND = {
    ctor: new NativeFunction(addr.ctor, "pointer", ["pointer"], "thiscall"),
    dtor: new NativeFunction(addr.dtor, "void", ["pointer"], "thiscall"),
    open: new NativeFunction(addr.open, "bool", ["pointer", "pointer", "bool", "bool"], "thiscall"),
    getSize: new NativeFunction(addr.getSize, "uint32", ["pointer"], "thiscall"),
    read: new NativeFunction(addr.read, "uint", ["pointer", "pointer", "uint32", "uint64"], "thiscall"),
  };
  return BOUND;
}

/** 启动时先绑定一次符号，符号缺失就在控制台留一行线索（不影响后续逐首解密）。 */
(function () {
  try {
    bindSymbols();
  } catch (e) {
    console.error("[TuneFree] " + e.message);
  }
})();

/* ---------- 内存：VirtualAlloc / VirtualFree ----------
 * 旧版用 Frida 的 Memory.alloc 分配整首，再用 Module.findExportByName(null, "free")
 * 去还。那个 free 是从所有已加载模块里碰运气搜到的，跟 Frida 自己的堆未必是同一个，
 * 还不上就静默失效 —— 每转一首地址空间只增不减。
 * VirtualAlloc(MEM_COMMIT | MEM_RESERVE) 一定拿到全新的地址段，
 * VirtualFree(MEM_RELEASE) 一定整段还给系统，没有堆碎片问题。
 */
var MEM_COMMIT = 0x1000;
var MEM_RESERVE = 0x2000;
var MEM_RELEASE = 0x8000;
var PAGE_READWRITE = 0x04;
var VALLOC = null;

var MEM_LOW_MSG =
  "QQ音乐的内存用满了（32 位进程地址空间上限约 4GB）。" +
  "请重启 QQ音乐，再点「开始转换」继续；已经转换好的文件会跳过，不会重做。";

function bindValloc() {
  if (VALLOC !== null) {
    return VALLOC;
  }
  var alloc = Module.findExportByName("kernel32.dll", "VirtualAlloc");
  var release = Module.findExportByName("kernel32.dll", "VirtualFree");
  if (alloc === null || release === null) {
    throw new Error("找不到 kernel32 的 VirtualAlloc，无法分配解密缓冲");
  }
  VALLOC = {
    alloc: new NativeFunction(alloc, "pointer", ["pointer", "uint32", "uint32", "uint32"]),
    free: new NativeFunction(release, "bool", ["pointer", "uint32", "uint32"]),
  };
  return VALLOC;
}

/** 申请一块可读写内存；地址空间不够时抛中文错误（调用方会原样显示给用户）。 */
function valloc(size) {
  var v = bindValloc();
  var p = v.alloc(ptr(0), size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
  if (p === null || p.equals(ptr(0))) {
    throw new Error(MEM_LOW_MSG);
  }
  return p;
}

/** 整段还给系统。失败也不影响已经写好的文件。 */
function vfree(p) {
  if (p === null) {
    return;
  }
  try {
    bindValloc().free(p, 0, MEM_RELEASE);
  } catch (e) {
    /* 还不了就算了 */
  }
}

/* ---------- 直接写盘 ----------
 * 老做法是把整首歌 readByteArray 拷到 Frida 侧，再让 Frida 写文件：
 * 几十 MB 要跨进程搬一次，写文件的是注入方，杀软和「慢」都出在这。
 * 这里改成在 QQ 音乐自己的进程里调 kernel32 写盘，数据不出进程。
 */
var GENERIC_WRITE = 0x40000000;
var CREATE_ALWAYS = 2;
var FILE_ATTRIBUTE_NORMAL = 0x80;
var WRITE_CHUNK = 4 * 1024 * 1024;
var K32 = null;

function bindKernel32() {
  if (K32 !== null) {
    return K32;
  }
  var create = Module.findExportByName("kernel32.dll", "CreateFileW");
  var write = Module.findExportByName("kernel32.dll", "WriteFile");
  var close = Module.findExportByName("kernel32.dll", "CloseHandle");
  if (create === null || write === null || close === null) {
    throw new Error("找不到 kernel32 的写入接口，退回 Frida 写盘");
  }
  K32 = {
    create: new NativeFunction(create, "pointer",
      ["pointer", "uint32", "uint32", "pointer", "uint32", "uint32", "pointer"]),
    write: new NativeFunction(write, "bool", ["pointer", "pointer", "uint32", "pointer", "pointer"]),
    close: new NativeFunction(close, "bool", ["pointer"]),
  };
  return K32;
}

/** 在目标进程里新建文件，返回句柄；失败抛错。 */
function createOutFile(path) {
  var k = bindKernel32();
  var wide = Memory.allocUtf16String(path);
  var handle = k.create(wide, GENERIC_WRITE, 0, ptr(0), CREATE_ALWAYS,
                        FILE_ATTRIBUTE_NORMAL, ptr(0));
  if (handle === null || handle.equals(ptr(0)) || handle.equals(ptr("-1"))) {
    throw new Error("QQ音乐写不了这个位置：" + path);
  }
  return handle;
}

/** 把目标进程里的这块内存直接写成文件，返回写入字节数。 */
function writeInProcess(buffer, size, path) {
  var k = bindKernel32();
  var handle = createOutFile(path);
  var done = Memory.alloc(4);
  var offset = 0;
  try {
    while (offset < size) {
      var want = Math.min(size - offset, WRITE_CHUNK);
      if (!k.write(handle, buffer.add(offset), want, done, ptr(0))) {
        throw new Error("写文件中途失败：" + path);
      }
      offset += want;
    }
  } finally {
    k.close(handle);
  }
  return offset;
}

/** 打开一个加密文件并取出大小，供下面几个入口复用。 */
function openSource(fns, obj, srcFileName) {
  var nameUtf16 = Memory.allocUtf16String(srcFileName);
  if (!fns.open(obj, nameUtf16, 1, 0)) {
    throw new Error(
      "打不开这个文件，可能还没下载完整，或者需要先在 QQ音乐里播放一遍再试：" + srcFileName
    );
  }
  var size = fns.getSize(obj);
  if (!size || size < 4) {
    throw new Error("读到的文件大小为 " + size + " 字节，文件可能已损坏：" + srcFileName);
  }
  if (size > MAX_FILE_SIZE) {
    throw new Error("文件过大（" + size + " 字节），超出 TuneFree 的处理范围：" + srcFileName);
  }
  return size;
}

/* ---------- 流式解密（默认路径） ----------
 * 只申请一块 4MB 的缓冲：读一块、写一块、复用，写完释放。
 * 峰值占用跟单曲大小无关，转几百首也不会把地址空间吃满。
 */
function decryptStream(srcFileName, dstFileName) {
  var fns = bindSymbols();
  var k = bindKernel32();
  var obj = Memory.alloc(OBJECT_SIZE);
  fns.ctor(obj);

  var buf = null;
  var handle = null;
  try {
    var size = openSource(fns, obj, srcFileName);
    handle = createOutFile(dstFileName);
    var done = Memory.alloc(4);
    buf = valloc(Math.min(size, READ_CHUNK));

    var offset = 0;
    while (offset < size) {
      var want = Math.min(size - offset, READ_CHUNK);
      var got = fns.read(obj, buf, want, offset);
      if (!got) {
        throw new Error("解密读取在第 " + offset + " 字节处中断：" + srcFileName);
      }
      if (!k.write(handle, buf, got, done, ptr(0))) {
        throw new Error("写文件中途失败：" + dstFileName);
      }
      offset += got;
    }
    if (offset !== size) {
      throw new Error(
        "解密出来的字节数不对（" + offset + " / " + size + "），文件可能不完整：" + srcFileName
      );
    }
    return offset;
  } finally {
    if (buf !== null) {
      vfree(buf);
    }
    if (handle !== null) {
      k.close(handle);
    }
    fns.dtor(obj);
  }
}

/* ---------- 整块解密（兜底路径） ----------
 * 保留给 decryptwhole 用：万一某个 QQ音乐 版本的 Read 不是随机访问，
 * 流式分块的偏移会对不上，Python 侧会退回到这里。
 */
function decryptToBuffer(srcFileName) {
  var fns = bindSymbols();
  var obj = Memory.alloc(OBJECT_SIZE);
  fns.ctor(obj);

  var buf = null;
  try {
    var size = openSource(fns, obj, srcFileName);
    buf = valloc(size);
    var offset = 0;
    while (offset < size) {
      var want = Math.min(size - offset, READ_CHUNK);
      var got = fns.read(obj, buf.add(offset), want, offset);
      if (!got) {
        break;
      }
      offset += got;
    }
    if (!offset) {
      throw new Error("解密读取返回 0 字节：" + srcFileName);
    }
    return { buffer: buf, size: offset };
  } catch (e) {
    if (buf !== null) {
      vfree(buf);
    }
    throw e;
  } finally {
    fns.dtor(obj);
  }
}

rpc.exports = {
  /**
   * 流式解密：读 4MB → 写 4MB，缓冲固定 4MB 复用。
   * @param {string} srcFileName 加密文件路径（.mflac / .mgg）
   * @param {string} dstFileName 解密结果写到哪（可以直接是中文名）
   * @returns {number} 实际写出的字节数
   */
  decrypt: function (srcFileName, dstFileName) {
    return decryptStream(srcFileName, dstFileName);
  },

  /**
   * 整块解密后写盘，流式失败时回退用。
   * 会把整首歌申请在内存里，转得多了仍然吃地址空间。
   */
  decryptwhole: function (srcFileName, dstFileName) {
    var part = decryptToBuffer(srcFileName);
    try {
      return writeInProcess(part.buffer, part.size, dstFileName);
    } finally {
      vfree(part.buffer);
    }
  },

  /**
   * 老写法：解密结果先读回 Frida 再写文件。
   * 只在进程内写盘失败时兜底（目标目录权限特殊、或者拿不到 kernel32）。
   */
  decryptfile: function (srcFileName, tmpFileName) {
    var part = decryptToBuffer(srcFileName);
    var data = null;
    try {
      data = part.buffer.readByteArray(part.size);
    } finally {
      vfree(part.buffer);
    }
    var tmpFile = new File(tmpFileName, "wb");
    try {
      tmpFile.write(data);
    } finally {
      tmpFile.flush();
      tmpFile.close();
    }
    return part.size;
  },

  /**
   * 只解密文件开头的一段，用于列表快速读取封面 / 时长，不必整首解密。
   * 缓冲用 VirtualAlloc 申请、读出来立刻释放 —— 旧版这里完全不释放，
   * 扫一遍上千首能白吃几百 MB。
   * @param {string} srcFileName 加密文件路径
   * @param {number} limit 最多读多少字节
   * @returns {ArrayBuffer} 解密后的字节
   */
  readhead: function (srcFileName, limit) {
    var fns = bindSymbols();
    var obj = Memory.alloc(OBJECT_SIZE);
    fns.ctor(obj);
    var buf = null;
    try {
      var nameUtf16 = Memory.allocUtf16String(srcFileName);
      if (!fns.open(obj, nameUtf16, 1, 0)) {
        throw new Error("打不开这个文件：" + srcFileName);
      }
      var size = fns.getSize(obj);
      if (!size) {
        throw new Error("读到的文件大小为 0：" + srcFileName);
      }
      var want = Math.min(size, limit || 262144);
      buf = valloc(want);
      var got = fns.read(obj, buf, want, 0);
      if (!got) {
        throw new Error("读取文件头返回 0 字节：" + srcFileName);
      }
      return buf.readByteArray(got);
    } finally {
      if (buf !== null) {
        vfree(buf);
      }
      fns.dtor(obj);
    }
  },

  /**
   * 只解密文件末尾的一段，用于 Ogg 尾部 granule 推算时长。
   * @returns {ArrayBuffer} 解密后的字节
   */
  readtail: function (srcFileName, limit) {
    var fns = bindSymbols();
    var obj = Memory.alloc(OBJECT_SIZE);
    fns.ctor(obj);
    var buf = null;
    try {
      var nameUtf16 = Memory.allocUtf16String(srcFileName);
      if (!fns.open(obj, nameUtf16, 1, 0)) {
        throw new Error("打不开这个文件：" + srcFileName);
      }
      var size = fns.getSize(obj);
      if (!size) {
        throw new Error("读到的文件大小为 0：" + srcFileName);
      }
      var want = Math.min(size, limit || 4096);
      var offset = size - want;
      buf = valloc(want);
      var got = fns.read(obj, buf, want, offset);
      if (!got) {
        throw new Error("读取文件尾返回 0 字节：" + srcFileName);
      }
      return buf.readByteArray(got);
    } finally {
      if (buf !== null) {
        vfree(buf);
      }
      fns.dtor(obj);
    }
  },

  /**
   * 地址空间余量探测：试着申请一块再立刻还回去。
   * 批量转换途中每转若干首问一次，失败就说明该重启 QQ音乐了。
   * @param {number} bytes 想申请的字节数
   * @returns {boolean} 申请成功为 true
   */
  probealloc: function (bytes) {
    try {
      var v = bindValloc();
      var size = bytes || 192 * 1024 * 1024;
      var p = v.alloc(ptr(0), size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
      if (p === null || p.equals(ptr(0))) {
        return false;
      }
      v.free(p, 0, MEM_RELEASE);
      return true;
    } catch (e) {
      return false;
    }
  },

  /** 供 main.py 做启动自检：能定位符号就返回 true，否则返回缺失清单。 */
  probe: function () {
    try {
      bindSymbols();
      return { ok: true, missing: [] };
    } catch (e) {
      return { ok: false, message: e.message };
    }
  },
};