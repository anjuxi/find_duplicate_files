# -*- coding: utf-8 -*-
# Find_duplicate_files 的单元测试，跑法：python -m unittest -v test_find_duplicates

import contextlib;
import io;
import json;
import os;
import tempfile;
import unittest;

from find_duplicate_files import find_duplicates, main;


class DuplicateFinderTest(unittest.TestCase):
    # 每个用例一个临时目录，跑完自动清掉。

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();
        self.root = self.tmp.name;

    def tearDown(self):
        self.tmp.cleanup();

    def write(self, rel_path, data, mtime=None):
        # 写文件，必要时把 mtime 钉死。删不删哪份跟时间有关，不能靠真实时钟碰运气。
        path = os.path.normpath(os.path.join(self.root, rel_path));
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True);
        with open(path, "wb") as fh:
            fh.write(data);
        if mtime is not None:
            os.utime(path, (mtime, mtime));
        return path;

    def groups_of(self, **kwargs):
        # 只关心“哪几个文件被分到一组”，路径排个序省得比对顺序。
        groups, _ = find_duplicates(self.root, jobs=4, **kwargs);
        return [sorted(os.path.normpath(path) for path, _ in items) for _, items in groups];

    def test_groups_same_content_across_dirs(self):
        # 目录和文件名都不同，内容一样就该归一组。
        first = self.write("a/report.txt", b"hello");
        second = self.write("b/report-copy.txt", b"hello");
        self.write("c/other.txt", b"world");
        self.assertEqual(self.groups_of(), [sorted([first, second])]);

    def test_rejects_same_size_different_content(self):
        self.write("a.txt", b"aaaa");
        self.write("b.txt", b"bbbb");
        self.assertEqual(self.groups_of(), []);

    def test_rejects_same_head_different_tail(self):
        # 前 64KB 一样、尾巴不同，得靠全文哈希才能拆开。
        head = b"x" * (64 * 1024);
        self.write("a.bin", head + b"tail-1");
        self.write("b.bin", head + b"tail-2");
        self.assertEqual(self.groups_of(), []);

    def test_min_and_max_size(self):
        self.write("small/a.txt", b"aa");
        self.write("small/b.txt", b"aa");
        self.write("big/a.bin", b"z" * 100);
        self.write("big/b.bin", b"z" * 100);
        self.assertEqual(len(self.groups_of(min_size=10)), 1);
        self.assertEqual(len(self.groups_of(max_size=10)), 1);
        self.assertEqual(len(self.groups_of(min_size=10, max_size=10)), 0);

    def test_exclude_and_hidden(self):
        self.write("keep/a.tmp", b"same");
        self.write("keep/b.tmp", b"same");
        self.write(".hidden/a.txt", b"same");
        self.write("visible/b.txt", b"same");
        self.assertEqual(self.groups_of(exclude=["*.tmp"], ignore_hidden=True), []);
        self.assertEqual(len(self.groups_of(exclude=["*.tmp"])), 1);

    def test_empty_files_grouped(self):
        # 空文件内容天然相同，默认会归一组，可释放 0 字节。
        first = self.write("a/empty", b"");
        second = self.write("b/empty", b"");
        self.assertEqual(self.groups_of(), [sorted([first, second])]);

    def test_hardlink_counted_once(self):
        # 硬链接指向同一个 inode，删了也省不出空间，只该算一个候选。
        first = self.write("a/source.txt", b"same");
        linked = os.path.join(self.root, "a", "hardlink.txt");
        try:
            os.link(first, linked);
        except OSError:
            self.skipTest("当前文件系统不支持硬链接");
        self.write("b/copy.txt", b"same");
        groups = self.groups_of();
        # 留的是原文件还是那个链接名，取决于遍历顺序，两种都算对。
        self.assertEqual(len(groups), 1);
        self.assertEqual(len(groups[0]), 2);

    def test_delete_keeps_oldest(self):
        old = self.write("keep.txt", b"same", mtime=1_000_000_000);
        new = self.write("drop.txt", b"same", mtime=1_600_000_000);
        with contextlib.redirect_stdout(io.StringIO()):
            code = main([self.root, "-d", "-y"]);
        self.assertEqual(code, 0);
        self.assertTrue(os.path.exists(old));
        self.assertFalse(os.path.exists(new));

    def test_hardlink_mode_shares_inode(self):
        keeper = self.write("keep.txt", b"same", mtime=1_000_000_000);
        duplicate = self.write("dup.txt", b"same", mtime=1_600_000_000);
        with contextlib.redirect_stdout(io.StringIO()):
            code = main([self.root, "-l", "-y"]);
        self.assertEqual(code, 0);
        self.assertTrue(os.path.exists(duplicate));
        self.assertEqual(os.stat(keeper).st_ino, os.stat(duplicate).st_ino);

    def test_json_to_stdout(self):
        # --json - 时 stdout 里只能有 JSON，人看的报告要走 stderr。
        self.write("a.txt", b"same");
        self.write("b.txt", b"same");
        buffer = io.StringIO();
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = main([self.root, "--json", "-"]);
        self.assertEqual(code, 0);
        data = json.loads(buffer.getvalue());
        self.assertEqual(data["summary"]["groups"], 1);
        self.assertEqual(data["summary"]["redundant"], 1);
        self.assertEqual(len(data["groups"][0]["files"]), 2);

    def test_missing_root_returns_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main([os.path.join(self.root, "no-such-dir")]);
        self.assertEqual(code, 2);

    def test_symlink_loop_is_skipped(self):
        # 子目录指回上级，扫描必须能自己停下来。
        self.write("a/x.txt", b"same");
        self.write("a/b/y.txt", b"same");
        try:
            os.symlink(self.root, os.path.join(self.root, "a", "loop"));
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接");
        self.assertEqual(len(self.groups_of()), 1);
        # 显式跟链接时，靠真实路径去重同样要挡住回路。
        self.assertEqual(len(self.groups_of(follow_links=True)), 1);


if __name__ == "__main__":
    unittest.main(verbosity=2);
