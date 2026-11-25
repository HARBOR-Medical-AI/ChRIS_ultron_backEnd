"""
The contract of the operations on paths that every storage manager keeps, mixed into
each manager's tests, which give it ``self.manager`` and delete what it leaves under
``test/``.
"""


class PathContractTests:
    """
    A path names an object or a folder, never the objects of a sibling whose name only
    starts with it, and what is not there counts as deleted.
    """

    def upload(self, *paths):
        for path in paths:
            self.manager.upload_obj(path, path.encode())

    def test_a_path_keeps_to_the_object_or_the_folder_it_names(self):
        manager = self.manager
        self.upload('test/sib/feed_1/a.txt', 'test/sib/feed_1/sub/b.txt',
                    'test/sib/feed_10/c.txt', 'test/sib/feed_1-x.txt')

        self.assertEqual(sorted(manager.ls('test/sib/feed_1')),
                         ['test/sib/feed_1/a.txt', 'test/sib/feed_1/sub/b.txt'])
        self.assertEqual(manager.ls('test/sib/feed_1-x.txt'), ['test/sib/feed_1-x.txt'])
        self.assertTrue(manager.path_exists('test/sib/feed_1'))
        self.assertTrue(manager.path_exists('test/sib/feed_1-x.txt'))
        self.assertFalse(manager.path_exists('test/sib/feed'))  # the start of names only

        manager.copy_path('test/sib/feed_1', 'test/sib/copy')
        self.assertEqual(sorted(manager.ls('test/sib/copy')),
                         ['test/sib/copy/a.txt', 'test/sib/copy/sub/b.txt'])

        manager.move_path('test/sib/copy', 'test/sib/moved')
        self.assertFalse(manager.path_exists('test/sib/copy'))
        self.assertEqual(manager.download_obj('test/sib/moved/sub/b.txt'),
                         b'test/sib/feed_1/sub/b.txt')

        manager.delete_path('test/sib/feed_1')
        self.assertFalse(manager.path_exists('test/sib/feed_1'))
        self.assertTrue(manager.obj_exists('test/sib/feed_10/c.txt'))
        self.assertTrue(manager.obj_exists('test/sib/feed_1-x.txt'))

        manager.delete_path('test/sib/feed_1-x.txt')  # an object
        self.assertFalse(manager.obj_exists('test/sib/feed_1-x.txt'))
        self.assertTrue(manager.obj_exists('test/sib/feed_10/c.txt'))

    def test_the_empty_path_names_nothing_and_a_slash_names_a_folder_only(self):
        manager = self.manager
        self.upload('test/f.txt', 'test/d/x')

        self.assertEqual(manager.ls(''), [])
        self.assertFalse(manager.path_exists(''))
        self.assertEqual(manager.ls('test/f.txt/'), [])
        self.assertFalse(manager.path_exists('test/f.txt/'))
        self.assertEqual(manager.ls('test/d/'), ['test/d/x'])
        self.assertTrue(manager.path_exists('test/d/'))

        manager.delete_path('test/f.txt/')  # a folder, which is not there
        self.assertTrue(manager.obj_exists('test/f.txt'))

    def test_copying_or_moving_what_is_not_there_does_nothing(self):
        manager = self.manager
        self.upload('test/f.txt')

        for operation in (manager.copy_path, manager.move_path):
            with self.subTest(operation=operation.__name__):
                operation('test/none/', 'test/other/')  # a folder no object was put in
                operation('test/none.txt', 'test/other.txt')
                operation('test/f.txt/', 'test/g/')  # a folder, which is not there

        self.assertFalse(manager.path_exists('test/other'))
        self.assertFalse(manager.path_exists('test/other.txt'))
        self.assertFalse(manager.path_exists('test/g'))
        self.assertTrue(manager.obj_exists('test/f.txt'))

    def test_deleting_what_is_not_there_is_no_error(self):
        self.upload('test/kept.txt')

        self.manager.delete_obj('test/none/a.txt')
        self.manager.delete_path('test/none')
        self.manager.delete_path('')   # names nothing, not the whole storage
        self.manager.delete_path('/')

        self.assertTrue(self.manager.obj_exists('test/kept.txt'))
