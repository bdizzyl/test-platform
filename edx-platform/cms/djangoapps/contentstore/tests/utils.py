# pylint: disable=E1101
'''
Utilities for contentstore tests
'''

import json

from django.test.client import Client
from django.contrib.auth.models import User

from xmodule.contentstore.django import contentstore
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.inheritance import own_metadata
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory
from xmodule.modulestore.xml_importer import import_from_xml
from student.models import Registration
from opaque_keys.edx.locations import SlashSeparatedCourseKey, AssetLocation
from contentstore.utils import reverse_url
from xmodule.modulestore.split_mongo.split import SplitMongoModuleStore


def parse_json(response):
    """Parse response, which is assumed to be json"""
    return json.loads(response.content)


def user(email):
    """look up a user by email"""
    return User.objects.get(email=email)


def registration(email):
    """look up registration object by email"""
    return Registration.objects.get(user__email=email)


class AjaxEnabledTestClient(Client):
    """
    Convenience class to make testing easier.
    """
    def ajax_post(self, path, data=None, content_type="application/json", **kwargs):
        """
        Convenience method for client post which serializes the data into json and sets the accept type
        to json
        """
        if not isinstance(data, basestring):
            data = json.dumps(data or {})
        kwargs.setdefault("HTTP_X_REQUESTED_WITH", "XMLHttpRequest")
        kwargs.setdefault("HTTP_ACCEPT", "application/json")
        return self.post(path=path, data=data, content_type=content_type, **kwargs)

    def get_html(self, path, data=None, follow=False, **extra):
        """
        Convenience method for client.get which sets the accept type to html
        """
        return self.get(path, data or {}, follow, HTTP_ACCEPT="text/html", **extra)

    def get_json(self, path, data=None, follow=False, **extra):
        """
        Convenience method for client.get which sets the accept type to json
        """
        return self.get(path, data or {}, follow, HTTP_ACCEPT="application/json", **extra)


class CourseTestCase(ModuleStoreTestCase):
    def setUp(self):
        """
        These tests need a user in the DB so that the django Test Client can log them in.
        The test user is created in the ModuleStoreTestCase setUp method.
        They inherit from the ModuleStoreTestCase class so that the mongodb collection
        will be cleared out before each test case execution and deleted
        afterwards.
        """
        user_password = super(CourseTestCase, self).setUp()

        self.client = AjaxEnabledTestClient()
        self.client.login(username=self.user.username, password=user_password)

        self.course = CourseFactory.create(
            org='MITx',
            number='999',
            display_name='Robot Super Course',
        )

    def create_non_staff_authed_user_client(self, authenticate=True):
        """
        Create a non-staff user, log them in (if authenticate=True), and return the client, user to use for testing.
        """
        nonstaff, password = self.create_non_staff_user()

        client = Client()
        if authenticate:
            client.login(username=nonstaff.username, password=password)
            nonstaff.is_authenticated = True
        return client, nonstaff

    def populate_course(self):
        """
        Add 2 chapters, 4 sections, 8 verticals, 16 problems to self.course (branching 2)
        """
        user_id = self.user.id
        def descend(parent, stack):
            xblock_type = stack.pop(0)
            for _ in range(2):
                child = ItemFactory.create(category=xblock_type, parent_location=parent.location, user_id=user_id)
                if stack:
                    descend(child, stack)

        descend(self.course, ['chapter', 'sequential', 'vertical', 'problem'])

    def reload_course(self):
        """
        Reloads the course object from the database
        """
        self.course = self.store.get_course(self.course.id)

    def save_course(self):
        """
        Updates the course object in the database
        """
        self.course.save()
        self.store.update_item(self.course, self.user.id)

    TEST_VERTICAL = 'vertical_test'
    PRIVATE_VERTICAL = 'a_private_vertical'
    PUBLISHED_VERTICAL = 'a_published_vertical'
    SEQUENTIAL = 'vertical_sequential'
    LOCKED_ASSET_KEY = AssetLocation.from_deprecated_string('/c4x/edX/toy/asset/sample_static.txt')

    def import_and_populate_course(self):
        """
        Imports the test toy course and populates it with additional test data
        """
        content_store = contentstore()
        import_from_xml(self.store, self.user.id, 'common/test/data/', ['toy'], static_content_store=content_store)
        course_id = SlashSeparatedCourseKey('edX', 'toy', '2012_Fall')

        # create an Orphan
        # We had a bug where orphaned draft nodes caused export to fail. This is here to cover that case.
        vertical = self.store.get_item(course_id.make_usage_key('vertical', self.TEST_VERTICAL), depth=1)
        vertical.location = vertical.location.replace(name='no_references')
        self.store.update_item(vertical, self.user.id, allow_not_found=True)
        orphan_vertical = self.store.get_item(vertical.location)
        self.assertEqual(orphan_vertical.location.name, 'no_references')
        self.assertEqual(len(orphan_vertical.children), len(vertical.children))

        # create a Draft vertical
        vertical = self.store.get_item(course_id.make_usage_key('vertical', self.TEST_VERTICAL), depth=1)
        draft_vertical = self.store.convert_to_draft(vertical.location, self.user.id)
        self.assertTrue(self.store.has_published_version(draft_vertical))

        # create a Private (draft only) vertical
        private_vertical = self.store.create_item(self.user.id, course_id, 'vertical', self.PRIVATE_VERTICAL)
        self.assertFalse(self.store.has_published_version(private_vertical))

        # create a Published (no draft) vertical
        public_vertical = self.store.create_item(self.user.id, course_id, 'vertical', self.PUBLISHED_VERTICAL)
        public_vertical = self.store.publish(public_vertical.location, self.user.id)
        self.assertTrue(self.store.has_published_version(public_vertical))

        # add the new private and new public as children of the sequential
        sequential = self.store.get_item(course_id.make_usage_key('sequential', self.SEQUENTIAL))
        sequential.children.append(private_vertical.location)
        sequential.children.append(public_vertical.location)
        self.store.update_item(sequential, self.user.id)

        # lock an asset
        content_store.set_attr(self.LOCKED_ASSET_KEY, 'locked', True)

        # create a non-portable link - should be rewritten in new courses
        html_module = self.store.get_item(course_id.make_usage_key('html', 'nonportable'))
        new_data = html_module.data = html_module.data.replace(
            '/static/',
            '/c4x/{0}/{1}/asset/'.format(course_id.org, course_id.course)
        )
        self.store.update_item(html_module, self.user.id)

        html_module = self.store.get_item(html_module.location)
        self.assertEqual(new_data, html_module.data)

        return course_id

    def check_populated_course(self, course_id):
        """
        Verifies the content of the given course, per data that was populated in import_and_populate_course
        """
        items = self.store.get_items(
            course_id,
            qualifiers={'category': 'vertical'},
            revision=ModuleStoreEnum.RevisionOption.published_only
        )
        self.check_verticals(items)

        def verify_item_publish_state(item, publish_state):
            """Verifies the publish state of the item is as expected."""
            self.assertEqual(self.store.has_published_version(item), publish_state)

        def get_and_verify_publish_state(item_type, item_name, publish_state):
            """Gets the given item from the store and verifies the publish state of the item is as expected."""
            item = self.store.get_item(course_id.make_usage_key(item_type, item_name))
            verify_item_publish_state(item, publish_state)
            return item

        # verify that the draft vertical is draft
        vertical = get_and_verify_publish_state('vertical', self.TEST_VERTICAL, True)
        for child in vertical.get_children():
            verify_item_publish_state(child, True)

        # make sure that we don't have a sequential that is not in draft mode
        sequential = get_and_verify_publish_state('sequential', self.SEQUENTIAL, True)

        # verify that we have the private vertical
        private_vertical = get_and_verify_publish_state('vertical', self.PRIVATE_VERTICAL, False)

        # verify that we have the public vertical
        public_vertical = get_and_verify_publish_state('vertical', self.PUBLISHED_VERTICAL, True)

        # verify verticals are children of sequential
        for vert in [vertical, private_vertical, public_vertical]:
            self.assertIn(vert.location, sequential.children)

        # verify textbook exists
        course = self.store.get_course(course_id)
        self.assertGreater(len(course.textbooks), 0)

        # verify asset attributes of locked asset key
        self.assertAssetsEqual(self.LOCKED_ASSET_KEY, self.LOCKED_ASSET_KEY.course_key, course_id)

        # verify non-portable links are rewritten
        html_module = self.store.get_item(course_id.make_usage_key('html', 'nonportable'))
        self.assertIn('/static/foo.jpg', html_module.data)

        return course

    def assertCoursesEqual(self, course1_id, course2_id):
        """
        Verifies the content of the two given courses are equal
        """
        course1_items = self.store.get_items(course1_id)
        course2_items = self.store.get_items(course2_id)
        self.assertGreater(len(course1_items), 0)  # ensure it found content instead of [] == []
        if len(course1_items) != len(course2_items):
            course1_block_ids = set([item.location.block_id for item in course1_items])
            course2_block_ids = set([item.location.block_id for item in course2_items])
            raise AssertionError(
                u"Course1 extra blocks: {}; course2 extra blocks: {}".format(
                    course1_block_ids - course2_block_ids, course2_block_ids - course1_block_ids
                )
            )

        for course1_item in course1_items:
            course1_item_loc = course1_item.location
            course2_item_loc = course2_id.make_usage_key(course1_item_loc.block_type, course1_item_loc.block_id)
            if course1_item_loc.block_type == 'course':
                # mongo uses the run as the name, split uses 'course'
                store = self.store._get_modulestore_for_courseid(course2_id)  # pylint: disable=protected-access
                new_name = 'course' if isinstance(store, SplitMongoModuleStore) else course2_item_loc.run
                course2_item_loc = course2_item_loc.replace(name=new_name)
            course2_item = self.store.get_item(course2_item_loc)

            # compare published state
            self.assertEqual(
                self.store.has_published_version(course1_item),
                self.store.has_published_version(course2_item)
            )

            # compare data
            self.assertEqual(hasattr(course1_item, 'data'), hasattr(course2_item, 'data'))
            if hasattr(course1_item, 'data'):
                self.assertEqual(course1_item.data, course2_item.data)

            # compare meta-data
            self.assertEqual(own_metadata(course1_item), own_metadata(course2_item))

            # compare children
            self.assertEqual(course1_item.has_children, course2_item.has_children)
            if course1_item.has_children:
                expected_children = []
                for course1_item_child in course1_item.children:
                    expected_children.append(
                        course2_id.make_usage_key(course1_item_child.block_type, course1_item_child.block_id)
                    )
                self.assertEqual(expected_children, course2_item.children)

        # compare assets
        content_store = self.store.contentstore
        course1_assets, count_course1_assets = content_store.get_all_content_for_course(course1_id)
        _, count_course2_assets = content_store.get_all_content_for_course(course2_id)
        self.assertEqual(count_course1_assets, count_course2_assets)
        for asset in course1_assets:
            asset_son = asset.get('content_son', asset['_id'])
            self.assertAssetsEqual(asset_son, course1_id, course2_id)

    def check_verticals(self, items):
        """ Test getting the editing HTML for each vertical. """
        # assert is here to make sure that the course being tested actually has verticals (units) to check.
        self.assertGreater(len(items), 0, "Course has no verticals (units) to check")
        for descriptor in items:
            resp = self.client.get_html(get_url('container_handler', descriptor.location))
            self.assertEqual(resp.status_code, 200)

    def assertAssetsEqual(self, asset_son, course1_id, course2_id):
        """Verifies the asset of the given key has the same attributes in both given courses."""
        content_store = contentstore()
        category = asset_son.block_type if hasattr(asset_son, 'block_type') else asset_son['category']
        filename = asset_son.block_id if hasattr(asset_son, 'block_id') else asset_son['name']
        course1_asset_attrs = content_store.get_attrs(course1_id.make_asset_key(category, filename))
        course2_asset_attrs = content_store.get_attrs(course2_id.make_asset_key(category, filename))
        self.assertEqual(len(course1_asset_attrs), len(course2_asset_attrs))
        for key, value in course1_asset_attrs.iteritems():
            if key in ['_id', 'filename', 'uploadDate', 'content_son', 'thumbnail_location']:
                pass
            else:
                self.assertEqual(value, course2_asset_attrs[key])


def get_url(handler_name, key_value, key_name='usage_key_string', kwargs=None):
    """
    Helper function for getting HTML for a page in Studio and checking that it does not error.
    """
    return reverse_url(handler_name, key_name, key_value, kwargs)
