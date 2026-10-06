"""forward_records.parse_record：微信偶发产出含裸 & 的 recordinfo（文章/CDN URL 未转义），
整体解析不能因此失败；合法实体（&amp; 等）不受影响。"""
import unittest

from integrations.forward_records import parse_record


class LooseAmpersandRecord(unittest.TestCase):
    RAW = ('<recordinfo><title>群聊的聊天记录</title><datalist count="2">'
           '<dataitem datatype="1" dataid="a"><sourcename>张三</sourcename>'
           '<datadesc>你好</datadesc></dataitem>'
           '<dataitem datatype="5" dataid="b"><sourcename>张三</sourcename>'
           '<link>https://mp.weixin.qq.com/s?__biz=MzA=&amp;mid=2247484466&idx=1&sn=abc#rd</link>'
           '<weburlitem><title>洛阳甩出AI王炸</title>'
           '<url>https://mp.weixin.qq.com/s?__biz=MzA=&mid=2247484466&idx=1#rd</url>'
           '</weburlitem></dataitem></datalist></recordinfo>')

    def test_bare_ampersands_in_link_urls_still_parse(self):
        items = parse_record(self.RAW)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['kind'], 'text')
        self.assertEqual(items[1]['kind'], 'link')
        self.assertIn('洛阳甩出AI王炸', items[1]['body'])
        self.assertIn('https://mp.weixin.qq.com/s?__biz=MzA=&mid=2247484466&idx=1#rd', items[1]['body'])

    def test_valid_entities_keep_their_meaning(self):
        raw = ('<datalist><dataitem datatype="1" dataid="a"><sourcename>张三</sourcename>'
               '<datadesc>a&lt;b&amp;c&gt;d</datadesc></dataitem></datalist>')
        self.assertEqual(parse_record(raw)[0]['body'], 'a<b&c>d')

    def test_double_escaped_stays_intact(self):
        raw = ('<datalist><dataitem datatype="1" dataid="a"><sourcename>张三</sourcename>'
               '<datadesc>x&amp;amp;y</datadesc></dataitem></datalist>')
        self.assertEqual(parse_record(raw)[0]['body'], 'x&amp;y')


if __name__ == '__main__':
    unittest.main()
