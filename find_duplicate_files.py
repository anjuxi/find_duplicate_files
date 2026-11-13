"""dupfind：按 SHA-256 找重复文件。

先用文件大小分组，再比前 64KB，最后全文哈希，逐级淘汰候选。
默认只看不动手，-d 删副本，-l 把副本换成硬链接。
"""

# ---------- 依赖导入 ----------
import argparse;
import fnmatch;
import hashlib;
import json;
import os;
import stat;
import sys;
import time;
from collections import defaultdict;
from concurrent.futures import ThreadPoolExecutor;

# ---------- 常量定义 ----------
V_分块字节 = 1 << 20;  # 1 MiB 一块，和常见文件系统的预读窗口差不多大。
V_头部字节 = 64 * 1024;  # 第一轮只读 64KB，同大小但内容不同的文件到这就被筛掉了。
V_默认线程 = min(32, (os.cpu_count() or 1) + 4);  # 哈希混着 IO 和计算，线程比核数多一点更合适。
V_重解析位 = 0x400;  # Windows 的 FILE_ATTRIBUTE_REPARSE_POINT，符号链接和目录联接都带这一位。
V_隐藏位 = 0x2;  # FILE_ATTRIBUTE_HIDDEN，和 Unix 的点号隐藏不是一回事。
V_并发下限 = 1 << 20;  # 一批文件总共读不到 1 MiB 时，开线程的调度开销比哈希本身还大。


# ---------- 工具函数 ----------
def report_error(action, path, error):
    # 单条错误只提示不中断。扫盘时碰到几个没权限的文件太常见，没必要整轮作废。
    print(f"{action} {path}: {error}！", file=sys.stderr);


def human_size(num):
    # 字节数换成人看的单位，不足 1 KiB 就直接给整数。
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024:
            return f"{int(num)} B" if unit == "B" else f"{num:.2f} {unit}";
        num /= 1024;
    return f"{num:.2f} PiB";


def is_hidden(name, attrs):
    # 点号开头算隐藏，这是 Unix 的习惯；Windows 还得看隐藏属性位。
    return name.startswith(".") or bool(attrs & V_隐藏位);


def is_excluded(rel_path, patterns):
    # 同时匹配相对路径和文件名，-x '*.tmp' 和 -x 'node_modules/*' 都写得出来。
    if not patterns:
        return False;
    normalized = rel_path.replace(os.sep, "/");
    name = normalized.rsplit("/", 1)[-1];
    for pattern in patterns:
        rule = pattern.replace(os.sep, "/");
        if fnmatch.fnmatchcase(normalized, rule) or fnmatch.fnmatchcase(name, rule):
            return True;
    return False;


# ---------- 扫描与哈希 ----------
def iter_files(root, follow_links=False, should_skip=None):
    # 自己压栈做深度优先，免得目录太深把递归撑爆。
    # 默认不跟链接：Windows 的目录联接 is_symlink() 返回假，只能看重解析位，
    # 漏掉这一项，一层层指回去就是死循环。
    stack = [(root, "")];
    # 跟链接时先把根目录记进去，子目录链回根目录才能立刻停。
    seen_dirs = {os.path.realpath(root)} if follow_links else set();
    while stack:
        current, rel = stack.pop();
        try:
            entries = os.scandir(current);
        except OSError as exc:
            report_error("跳过目录", current, exc);
            continue;
        with entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False);
                except OSError as exc:
                    report_error("跳过", entry.path, exc);
                    continue;
                attrs = getattr(st, "st_file_attributes", 0);
                is_link = entry.is_symlink() or bool(attrs & V_重解析位);
                if is_link and not follow_links:
                    continue;
                child_rel = f"{rel}{os.sep}{entry.name}" if rel else entry.name;
                if entry.is_dir(follow_symlinks=follow_links):
                    if should_skip and should_skip(child_rel, entry.name, True, attrs):
                        continue;
                    if follow_links:
                        # 跟链接时按真实路径去重。只认链接不够，普通目录也得记，
                        # 否则一个指向已扫目录的链接会把同一批文件再算一遍。
                        real = os.path.realpath(entry.path);
                        if real in seen_dirs:
                            continue;
                        seen_dirs.add(real);
                    stack.append((entry.path, child_rel));
                elif entry.is_file(follow_symlinks=follow_links):
                    if should_skip and should_skip(child_rel, entry.name, False, attrs):
                        continue;
                    if is_link:
                        try:
                            st = entry.stat(follow_symlinks=True);
                        except OSError as exc:
                            report_error("跳过", entry.path, exc);
                            continue;
                    yield (entry.path, st.st_size, st.st_mtime_ns,
                           st.st_dev, st.st_ino, attrs);


def file_hash(path, limit=None):
    # limit 是只读多少字节，None 读全文。读不全、没权限、读完发现文件变了，一律返回 None。
    try:
        before = os.stat(path);
        with open(path, "rb") as fh:
            if limit is None and hasattr(hashlib, "file_digest"):
                digest = hashlib.file_digest(fh, "sha256").digest();
            else:
                hasher = hashlib.sha256();
                left = limit;
                while left is None or left > 0:
                    size = V_分块字节 if left is None else min(V_分块字节, left);
                    data = fh.read(size);
                    if not data:
                        break;
                    hasher.update(data);
                    if left is not None:
                        left -= len(data);
                if left:
                    return None;  # 文件比预期短，这轮结果不算数。
                digest = hasher.digest();
        after = os.stat(path);
    except OSError as exc:
        report_error("跳过", path, exc);
        return None;
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        return None;  # 算哈希时文件被改过，宁可漏报也不能拿这份结果去删东西。
    return digest;


def hash_many(paths, limit, pool=None):
    # 小批次直接串行。一个组往往就两三个文件，走线程池反而更慢。
    if pool is None or len(paths) < 2:
        return {path: file_hash(path, limit) for path in paths};
    if limit is not None and limit * len(paths) < V_并发下限:
        return {path: file_hash(path, limit) for path in paths};
    return dict(zip(paths, pool.map(lambda p: file_hash(p, limit), paths)));


def drop_hardlinks(items):
    # 同一个 inode 的几个路径，删哪个都省不出空间，只留一个。
    # Windows 上 scandir 给的 ino 是 0，只有大小撞车时才补一次 os.stat，量不大。
    unique, seen = [], set();
    for path, mtime, dev, ino in items:
        if not ino:
            try:
                st = os.stat(path);
            except OSError as exc:
                report_error("跳过", path, exc);
                continue;
            dev, ino = st.st_dev, st.st_ino;
        if ino:
            key = (dev, ino);
            if key in seen:
                continue;
            seen.add(key);
        unique.append((path, mtime));
    return unique;


def find_duplicates(root, min_size=0, max_size=None, exclude=(), ignore_hidden=False,
                    follow_links=False, jobs=V_默认线程, progress=None):
    # 返回 (重复组, 扫描文件数)。每个组是 (大小, [(路径, 修改时间), ...])。
    def should_skip(rel_path, name, is_dir, attrs):
        # 排除规则放在遍历里，命中 node_modules 这类目录能顺手剪掉整棵子树。
        if ignore_hidden and is_hidden(name, attrs):
            return True;
        return is_excluded(rel_path, exclude);

    by_size = defaultdict(list);
    total = 0;
    for path, size, mtime, dev, ino, _ in iter_files(root, follow_links, should_skip):
        if size < min_size:
            continue;
        if max_size is not None and size > max_size:
            continue;
        by_size[size].append((path, mtime, dev, ino));
        total += 1;
        if progress and total % 512 == 0:
            progress(total);

    groups = [];
    # 整轮只建一个池子。以前每组新建一个，组一多光线程初始化就够呛。
    pool = ThreadPoolExecutor(max_workers=jobs) if jobs > 1 else None;
    try:
        for size, items in by_size.items():
            if len(items) < 2:
                continue;  # 大小独一份，后面的哈希都省了。
            unique = drop_hardlinks(items);
            if len(unique) < 2:
                continue;

            paths = [path for path, _ in unique];
            mtimes = {path: mtime for path, mtime in unique};

            # 第一轮只读文件头。文件不超过 64KB 时，这一步就是全文哈希。
            digests = hash_many(paths, min(size, V_头部字节), pool);
            same_head = defaultdict(list);
            for path in paths:
                if digests[path] is not None:
                    same_head[digests[path]].append(path);

            for candidates in same_head.values():
                if len(candidates) < 2:
                    continue;
                if size <= V_头部字节:
                    groups.append((size, [(path, mtimes[path]) for path in candidates]));
                    continue;
                full = hash_many(candidates, size, pool);
                same_all = defaultdict(list);
                for path in candidates:
                    if full[path] is not None:
                        same_all[full[path]].append(path);
                for matched in same_all.values():
                    if len(matched) > 1:
                        groups.append((size, [(path, mtimes[path]) for path in matched]));
    finally:
        if pool is not None:
            pool.shutdown();

    return groups, total;


# ---------- 结果排序与动作 ----------
def sort_group(items, keep):
    # 组内排个序，第一项就是保留项，后面的都是待处理的副本。
    if keep == "newest":
        items.sort(key=lambda item: (-item[1], item[0]));
    elif keep == "shortest":
        items.sort(key=lambda item: (len(item[0]), item[0]));
    else:
        items.sort(key=lambda item: (item[1], item[0]));


def remove_copy(path):
    # 只读文件先去掉属性再删一次，Windows 上直接 os.remove 会被拒。
    try:
        os.remove(path);
        return None;
    except PermissionError:
        pass;
    except OSError as exc:
        return exc;
    try:
        os.chmod(path, stat.S_IWRITE);
        os.remove(path);
    except OSError as exc:
        return exc;
    return None;


def replace_with_hardlink(keeper, duplicate):
    # 先链到临时名再替换。反过来的话，一旦链接失败，副本已经没了。
    temp = f"{duplicate}.dupfind-{os.getpid()}.tmp";
    try:
        os.link(keeper, temp);
        try:
            os.replace(temp, duplicate);
        except PermissionError:
            os.chmod(duplicate, stat.S_IWRITE);
            os.replace(temp, duplicate);
    except OSError as exc:
        try:
            os.remove(temp);
        except OSError:
            pass;
        return exc;
    return None;


# ---------- 输出 ----------
def print_report(groups, stream=sys.stdout):
    # 打印分组，顺便把可释放空间和冗余副本数算出来给 main 用。
    wasted = redundant = 0;
    for index, (size, items) in enumerate(groups, 1):
        group_waste = size * (len(items) - 1);
        wasted += group_waste;
        redundant += len(items) - 1;
        print(f"== 重复组 {index}: {len(items)} 个文件，每个 {human_size(size)}，"
              f"可释放 {human_size(group_waste)}！", file=stream);
        for position, (path, _) in enumerate(items):
            mark = "  [保留]" if position == 0 else "";
            print(f"   {path}{mark}！", file=stream);
        print(file=stream);
    return wasted, redundant;


def emit_json(groups, root, total, wasted, redundant, stream):
    # 给二次处理用的结果，路径保持原样，mtime 用纳秒整数免得到处丢精度。
    payload = {
        "root": os.path.abspath(root),
        "files_scanned": total,
        "summary": {
            "groups": len(groups),
            "redundant": redundant,
            "wasted": wasted,
        },
        "groups": [
            {
                "size": size,
                "wasted": size * (len(items) - 1),
                "files": [
                    {"path": path, "mtime_ns": mtime, "keep": position == 0}
                    for position, (path, mtime) in enumerate(items)
                ],
            }
            for size, items in groups
        ],
    };
    json.dump(payload, stream, ensure_ascii=False, indent=2);
    stream.write("\n");


# ---------- 命令行入口 ----------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="dupfind",
        description="按 SHA-256 查找重复文件（默认递归扫描当前目录）");
    parser.add_argument("root", nargs="?", default=".", help="扫描目录，默认 '.'");
    parser.add_argument("-m", "--min-size", type=int, default=0,
                        help="忽略小于该字节数的文件");
    parser.add_argument("-M", "--max-size", type=int, default=None,
                        help="忽略大于该字节数的文件");
    parser.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                        help="排除匹配的路径，可重复使用，如 -x '*.tmp'");
    parser.add_argument("--ignore-hidden", action="store_true",
                        help="跳过隐藏文件和隐藏目录");
    parser.add_argument("--follow-links", action="store_true",
                        help="跟随符号链接和目录联接（默认跳过）");
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help=f"哈希线程数，0 用默认值 {V_默认线程}");
    parser.add_argument("-k", "--keep", choices=("oldest", "newest", "shortest"),
                        default="oldest", help="组内保留哪一份，默认最早修改的");
    action = parser.add_mutually_exclusive_group();
    action.add_argument("-d", "--delete", action="store_true",
                        help="删除每组除保留项外的副本");
    action.add_argument("-l", "--hardlink", action="store_true",
                        help="把副本换成指向保留项的硬链接");
    parser.add_argument("-y", "--yes", action="store_true", help="删除或替换时不问确认");
    parser.add_argument("--json", metavar="FILE", default=None,
                        help="把结果写成 JSON 文件，'-' 表示输出到 stdout");
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印扫描进度和统计");
    return parser;


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace");
        except (AttributeError, ValueError, OSError):
            pass;

    args = build_parser().parse_args(argv);
    # JSON 走 stdout 时，人看的报告和动作日志全部挪到 stderr，管道里只留 JSON。
    out = sys.stderr if args.json == "-" else sys.stdout;
    if not os.path.isdir(args.root):
        print(f"目录不存在或不是目录: {args.root}！", file=sys.stderr);
        return 2;
    if args.min_size < 0 or (args.max_size is not None and args.max_size < 0):
        print("大小过滤不能是负数！", file=sys.stderr);
        return 2;

    def progress(count):
        print(f"已扫描 {count} 个文件...", file=sys.stderr);

    started = time.monotonic();
    groups, total = find_duplicates(
        args.root,
        min_size=args.min_size,
        max_size=args.max_size,
        exclude=args.exclude,
        ignore_hidden=args.ignore_hidden,
        follow_links=args.follow_links,
        jobs=V_默认线程 if args.jobs == 0 else max(1, args.jobs),
        progress=progress if args.verbose else None,
    );

    # 先按保留策略定组内顺序，再按可释放空间排组。两步都是确定的，同一棵树跑几遍结果一样。
    for _, items in groups:
        sort_group(items, args.keep);
    groups.sort(key=lambda group: (-(group[0] * (len(group[1]) - 1)), group[0]));

    if args.verbose:
        print(f"扫描 {total} 个文件，{len(groups)} 组重复，"
              f"耗时 {time.monotonic() - started:.2f} 秒！", file=sys.stderr);

    if not groups:
        print("未发现重复文件！", file=out);
        if args.json:
            if args.json == "-":
                emit_json([], args.root, total, 0, 0, sys.stdout);
            else:
                try:
                    with open(args.json, "w", encoding="utf-8") as fh:
                        emit_json([], args.root, total, 0, 0, fh);
                except OSError as exc:
                    print(f"写 JSON 失败: {exc}！", file=sys.stderr);
                    return 2;
        return 0;

    wasted, redundant = print_report(groups, out);

    if args.json:
        try:
            if args.json == "-":
                emit_json(groups, args.root, total, wasted, redundant, sys.stdout);
            else:
                with open(args.json, "w", encoding="utf-8") as fh:
                    emit_json(groups, args.root, total, wasted, redundant, fh);
        except OSError as exc:
            print(f"写 JSON 失败: {exc}！", file=sys.stderr);
            return 2;

    failed = 0;
    if args.delete or args.hardlink:
        verb = "删除" if args.delete else "替换为硬链接";
        if not args.yes:
            try:
                # 提示语走 stderr，不然 --json - 的管道里会混进一行问话。
                # stdin 接 NUL 设备时 isatty() 在 Windows 上还是真，读不出内容就取消。
                if not sys.stdin.isatty():
                    raise EOFError;
                print(f"{verb} {redundant} 个冗余副本？[y/N] ", end="",
                      file=sys.stderr, flush=True);
                answer = sys.stdin.readline().strip().lower();
            except (EOFError, OSError, KeyboardInterrupt):
                print("操作取消：无法交互确认，请加 -y！", file=sys.stderr);
                return 2;
            if answer not in ("y", "yes"):
                print("已取消！", file=out);
                return 0;

        for _, items in groups:
            keeper = items[0][0];
            for path, _ in items[1:]:
                error = remove_copy(path) if args.delete else replace_with_hardlink(keeper, path);
                if error is None:
                    print(f"   已{verb} {path}！", file=out);
                else:
                    failed += 1;
                    print(f"   {verb}失败 {path}: {error}！", file=sys.stderr);

    print("——" * 50, file=out);
    print(f"✅ 共 {len(groups)} 组重复，{redundant} 个冗余副本，"
          f"可释放 {human_size(wasted)}！", file=out);
    return 1 if failed else 0;


if __name__ == "__main__":
    sys.exit(main());
