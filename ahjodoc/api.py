import json
import urlparse
import os
from django.conf.urls import url
from django.contrib.gis.geos import Polygon
from django.core.paginator import Paginator, InvalidPage
from django.utils.html import strip_tags
from django.conf import settings
from django.db.models import Count, Sum
from django.http import Http404
from tastypie import fields
from tastypie.resources import ModelResource
from tastypie.exceptions import InvalidFilterError, BadRequest, NotFound
from tastypie.constants import ALL, ALL_WITH_RELATIONS
from tastypie.cache import SimpleCache
from tastypie.contrib.gis.resources import ModelResource as GeoModelResource
from tastypie.utils import trailing_slash
from ahjodoc.models import *
from decisions.models import Organization
from haystack.query import SearchQuerySet
from haystack.utils.geo import Point as HaystackPoint

CACHE_TIMEOUT = 600

class PolicymakerResource(ModelResource):
    def apply_filters(self, request, filters):
        qs = super(PolicymakerResource, self).apply_filters(request, filters)

        # Do not show office holders by default
        if request.GET.get('show_office_holders', '').lower() not in ('1', 'true'):
            qs = qs.exclude(type='office_holder')

        meetings = request.GET.get('meetings', '')
        if meetings.lower() in ('1', 'true'):
            # Include only categories with associated issues
            qs = qs.annotate(num_meetings=Count('meeting')).filter(num_meetings__gt=0)
        return qs

    def dehydrate(self, bundle):
        obj = bundle.obj
        org = obj.organization
        bundle.data['org_type'] = org.type
        bundle.data['dissolution_date'] = org.dissolution_date
        bundle.data['founding_date'] = org.founding_date
        return bundle

    class Meta:
        queryset = Policymaker.objects.all().select_related('organization')
        resource_name = 'policymaker'
        filtering = {
            'abbreviation': ('exact', 'in', 'isnull'),
            'name': ALL,
            'origin_id': ('exact', 'in'),
            'slug': ('exact', 'in')
        }
        ordering = ('name',)
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class CategoryResource(ModelResource):
    parent = fields.ToOneField('self', 'parent', null=True)

    def query_to_filters(self, query):
        filters = {}
        filters['name__icontains'] = query
        return filters

    def build_filters(self, filters=None):
        orm_filters = super(CategoryResource, self).build_filters(filters)
        if filters and 'input' in filters:
            orm_filters.update(self.query_to_filters(filters['input']))
        return orm_filters

    def apply_filters(self, request, filters):
        qs = super(CategoryResource, self).apply_filters(request, filters)
        issues = request.GET.get('issues', '')
        if issues.lower() in ('1', 'true'):
            # Include only categories with associated issues
            qs = qs.annotate(num_issues=Count('issue')).filter(num_issues__gt=0)
        return qs
    def dehydrate(self, bundle):
        if hasattr(bundle.obj, 'num_issues'):
            bundle.data['num_issues'] = bundle.obj.num_issues
        return bundle

    class Meta:
        queryset = Category.objects.all()
        excludes = ['lft', 'rght', 'tree_id']
        resource_name = 'category'
        filtering = {
            'level': ALL,
            'name': ALL,
            'origin_id': ALL,
        }
        ordering = ['level', 'name', 'origin_id']
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class MeetingResource(ModelResource):
    policymaker = fields.ToOneField(PolicymakerResource, 'policymaker')

    def dehydrate(self, bundle):
        obj = bundle.obj
        bundle.data['policymaker_name'] = obj.policymaker.name
        return bundle

    class Meta:
        queryset = Meeting.objects.order_by('-date').select_related('policymaker')
        resource_name = 'meeting'
        filtering = {
            'policymaker': ALL_WITH_RELATIONS,
            'minutes': ('exact',)
        }
        ordering = ('date', 'policymaker')
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class MeetingDocumentResource(ModelResource):
    meeting = fields.ToOneField(MeetingResource, 'meeting', full=True)
    xml_uri = fields.CharField()

    def dehydrate_xml_uri(self, bundle):
        uri = bundle.obj.xml_file.url
        if bundle.request:
            uri = bundle.request.build_absolute_uri(uri)
        return uri

    class Meta:
        queryset = MeetingDocument.objects.order_by('-last_modified_time')
        resource_name = 'meeting_document'
        excludes = ['xml_file']
        filtering = {
            'type': ALL,
            'meeting': ALL_WITH_RELATIONS,
            'publish_time': ALL,
            'date': ALL
        }
        ordering = ('date', 'publish_time')
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

def poly_from_bbox(bbox_val):
    points = bbox_val.split(',')
    if len(points) != 4:
        raise InvalidFilterError("bbox must be in format 'left,bottom,right,top'")
    try:
        points = [float(p) for p in points]
    except ValueError:
        raise InvalidFilterError("bbox values must be floating point")
    poly = Polygon.from_bbox(points)
    return poly

def build_bbox_filter(bbox_val, field_name):
    poly = poly_from_bbox(bbox_val)
    return {"%s__within" % field_name: poly}

class IssueResource(ModelResource):
    category = fields.ToOneField(CategoryResource, 'category')

    def prepend_urls(self):
        return [
            url(r"^(?P<resource_name>%s)/search%s$" % (self._meta.resource_name, trailing_slash()), self.wrap_view('get_search'), name="api_get_search"),
        ]

    def apply_filters(self, request, applicable_filters):
        ret = super(IssueResource, self).apply_filters(request, applicable_filters)
        for f in ('issuegeometry__in', 'districts__name', 'districts__type', 'geometries__isnull'):
            if f in applicable_filters:
                ret = ret.distinct()
                break
        return ret

    def build_filters(self, filters=None):
        orm_filters = super(IssueResource, self).build_filters(filters)
        if not filters:
            return orm_filters

        if 'bbox' in filters:
            bbox_filter = build_bbox_filter(filters['bbox'], 'geometry')
            geom_list = IssueGeometry.objects.filter(**bbox_filter)
            orm_filters['issuegeometry__in'] = geom_list

        has_geometry = filters.get('has_geometry', '')
        if has_geometry.lower() in ['1', 'true']:
            orm_filters['geometries__isnull'] = False

        district_filters = ['districts__name', 'districts__type']
        for f in district_filters:
            if f in filters:
                orm_filters[f] = filters[f]

        return orm_filters

    def get_search(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        self.is_authenticated(request)
        self.throttle_check(request)

        try:
            page_count = min(int(request.GET.get('limit', 20)), 500)
            page_nr = int(request.GET.get('page', 1))
            if page_count <= 0 or page_nr <= 0:
                raise ValueError()
        except ValueError:
            raise BadRequest("'limit' and 'page' must be positive integers")

        sqs = SearchQuerySet().models(Issue).load_all()
        query = request.GET.get('text', '').strip()
        if query:
            sqs = sqs.auto_query(query).highlight()
            order_by = None
        else:
            order_by = '-latest_decision_date'

        s = request.GET.get('order_by', '').lower()
        if s:
            if s[0] == '-':
                reverse = True
                s = s[1:]
            else:
                reverse = False

            if s not in ('latest_decision_date', 'relevance'):
                raise BadRequest("'order_by' must either be for 'latest_decision_date' or 'relevance'")
            if reverse:
                order_by = '-' + s
            else:
                order_by = s
            if s == 'relevance':
                order_by = None

        if order_by:
            sqs = sqs.order_by(order_by)

        category = request.GET.get('category', '').strip()
        if category:
            try:
                cat_nr = int(category)
            except ValueError:
                raise BadRequest("'category' must be a positive integer")
            # Search in all ancestor categories, too
            sqs = sqs.filter(categories=cat_nr)

        district = request.GET.get('district', '').strip()
        if district:
            d_list = district.split(',')
            sqs = sqs.filter(districts__in=d_list)

        bbox = request.GET.get('bbox', '').strip()
        if bbox:
            poly = poly_from_bbox(bbox)
            e = poly.extent
            bottom_left = HaystackPoint(e[0], e[1])
            top_right = HaystackPoint(e[2], e[3])
            sqs = sqs.within('location', bottom_left, top_right)

        policymaker = request.GET.get('policymaker', '').strip()
        if policymaker:
            pm_list = policymaker.split(',')
            try:
                pm_list = [int(x) for x in pm_list]
            except ValueError:
                raise BadRequest("'policymaker' must be a list of positive integers")
            sqs = sqs.filter(policymakers__in=pm_list)

        paginator = Paginator(sqs, page_count)
        try:
            page = paginator.page(page_nr)
        except InvalidPage:
            raise Http404("Sorry, no results on that page.")

        objects = []

        for result in page.object_list:
            bundle = self.build_bundle(obj=result.object, request=request)
            bundle = self.full_dehydrate(bundle)
            if result.highlighted and 'text' in result.highlighted:
                bundle.data['search_highlighted'] = result.highlighted['text'][0]
            objects.append(bundle)

        total_count = sqs.count()

        object_list = {
            'objects': objects,
            'meta': {'page': page_nr, 'limit': page_count, 'total_count': total_count}
        }

        self.log_throttled_access(request)
        return self.create_response(request, object_list)

    def dehydrate(self, bundle):
        obj = bundle.obj
        bundle.data['category_origin_id'] = obj.category.origin_id
        bundle.data['category_name'] = obj.category.name
        bundle.data['top_category_name'] = obj.category.get_root().name
        summary = obj.determine_summary()
        if summary:
            bundle.data['summary'] = summary

        if bundle.request.GET.get('no_geometry', '').lower() not in ('1', 'true'):
            geometries = []
            for geom in obj.geometries.all():
                d = json.loads(geom.geometry.geojson)
                d['name'] = geom.name
                d['category'] = geom.type
                geometries.append(d)
            bundle.data['geometries'] = geometries

        districts = []
        for d in obj.districts.all():
            districts.append({'name': d.name, 'type': d.type})
        bundle.data['districts'] = districts

        return bundle

    class Meta:
        queryset = Issue.objects.all().select_related('category')
        resource_name = 'issue'
        excludes = ['origin_id']
        filtering = {
            'register_id': ALL,
            'slug': ALL,
            'category': ALL_WITH_RELATIONS,
        }
        ordering = ('latest_decision_date', 'last_modified_time')
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class IssueGeometryResource(ModelResource):
    issue = fields.ToOneField(IssueResource, 'issue')

    class Meta:
        queryset = IssueGeometry.objects.all()
        resource_name = 'issue_geometry'
        filtering = {
            'issue': ALL_WITH_RELATIONS
        }
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class AgendaItemResource(ModelResource):
    meeting = fields.ToOneField(MeetingResource, 'meeting', full=True)
    issue = fields.ToOneField(IssueResource, 'issue', full=True)
    attachments = fields.ToManyField('ahjodoc.api.AttachmentResource', 'attachment_set', full=True, null=True)

    def dehydrate(self, bundle):
        obj = bundle.obj
        cs_list = ContentSection.objects.filter(agenda_item=obj)
        content = []
        for cs in cs_list:
            d = {'type': cs.type, 'text': cs.text}
            content.append(d)
        bundle.data['content'] = content
        bundle.data['permalink'] = bundle.request.build_absolute_uri(obj.get_absolute_url())
        return bundle

    class Meta:
        queryset = AgendaItem.objects.all().select_related('issue').select_related('category').select_related('attachments')
        resource_name = 'agenda_item'
        filtering = {
            'meeting': ALL_WITH_RELATIONS,
            'issue': ['exact', 'in'],
            'issue__category': ['exact', 'in'],
            'last_modified_time': ['gt', 'gte', 'lt', 'lte'],
            'from_minutes': ['exact'],
            'resolution': ['exact', 'isnull', 'in'],
        }
        ordering = ('last_modified_time', 'origin_last_modified_time', 'meeting', 'index')
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class AttachmentResource(ModelResource):
    agenda_item = fields.ToOneField(AgendaItemResource, 'agenda_item')
    file_uri = fields.CharField(null=True)

    def dehydrate_file_uri(self, bundle):
        if not bundle.obj.file:
            return None
        uri = bundle.obj.file.url
        if bundle.request:
            uri = bundle.request.build_absolute_uri(uri)
        return uri

    class Meta:
        queryset = Attachment.objects.all()
        resource_name = 'attachment'
        excludes = ['file']
        filtering = {
            'agenda_item': ALL_WITH_RELATIONS,
            'hash': ALL,
            'number': ALL,
        }
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

class VideoResource(ModelResource):
    meeting = fields.ToOneField(MeetingResource, 'meeting')
    agenda_item = fields.ToOneField(AgendaItemResource, 'agenda_item', null=True)
    screenshot_uri = fields.CharField()

    def dehydrate_screenshot_uri(self, bundle):
        uri = bundle.obj.screenshot.url
        if bundle.request:
            uri = bundle.request.build_absolute_uri(uri)
        return uri

    def dehydrate(self, bundle):
        # A quick hack to provide links to the .OGG versions
        fname = bundle.obj.url.split('/')[-1]
        if fname.split('.')[-1] != 'mp4':
            return bundle
        base_path = os.path.join(settings.MEDIA_ROOT, settings.AHJO_PATHS['video'])
        local_copies = {}
        uri_path = os.path.join(settings.MEDIA_URL, settings.AHJO_PATHS['video'])
        if os.path.exists(os.path.join(base_path, fname)):
            uri = bundle.request.build_absolute_uri('%s/%s' % (uri_path, fname))
            local_copies['video/mp4'] = uri
        fname = '.'.join(fname.split('.')[:-1] + ['ogv'])
        if os.path.exists(os.path.join(base_path, fname)):
            uri = bundle.request.build_absolute_uri('%s/%s' % (uri_path, fname))
            local_copies['video/ogg'] = uri
        if local_copies:
            bundle.data['local_copies'] = local_copies
        return bundle

    class Meta:
        queryset = Video.objects.all()
        excludes = ['screenshot']
        filtering = {
            'meeting': ALL_WITH_RELATIONS,
            'agenda_item': ALL_WITH_RELATIONS,
            'index': ALL,
            'speaker': ['exact'],
        }
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)


# Introduce a new version of ToManyField that keeps track if we're
# dehydrating a nested resource (bundle.related) or not.
class ToManyField(fields.ToManyField):
    def dehydrate_related(self, bundle, related_resource, for_list=True):
        """
        Based on the ``full_resource``, returns either the endpoint or the data
        from ``full_dehydrate`` for the related resource.
        """
        should_dehydrate_full_resource = self.should_full_dehydrate(bundle, for_list=for_list)

        if not should_dehydrate_full_resource:
            # Be a good netizen.
            return related_resource.get_resource_uri(bundle)
        else:
            # ZOMG extra data and big payloads.
            bundle = related_resource.build_bundle(
                obj=related_resource.instance,
                request=bundle.request,
                objects_saved=bundle.objects_saved
            )
            bundle.related = True
            return related_resource.full_dehydrate(bundle)

class OrganizationResource(ModelResource):
    parents = ToManyField('self', 'parents', full=True,
                          full_detail=True, full_list=False)
    policymaker = fields.ToOneField(PolicymakerResource, 'policymaker', full=False, null=True)

    def _get_ancestors(self, org, id_list):
        parents = org.parents.only('id')
        for p in parents:
            id_list.append(p.id)
            self._get_ancestors(p, id_list)

    def dehydrate(self, bundle):
        if bundle.obj.policymaker:
            bundle.data['policymaker_slug'] = bundle.obj.policymaker.slug
        children = bundle.request.GET.get('children', '')
        if children.lower() in ['1', 'true'] and not hasattr(bundle, 'related'):
            bundles = []
            children_list = bundle.obj.all_children.all()
            req = bundle.request
            for obj in children_list:
                c_bundle = self.build_bundle(obj=obj, request=req)
                c_bundle.related = True
                bundles.append(self.full_dehydrate(c_bundle, for_list=True))
            bundle.data['children'] = bundles

        return bundle

    def apply_filters(self, request, filters):
        qs = super(OrganizationResource, self).apply_filters(request, filters)

        show_dissolved = request.GET.get('show_dissolved', '').lower()
        if show_dissolved not in ('1', 'true'):
            qs = qs.filter(dissolution_date=None)

        return qs

    class Meta:
        queryset = Organization.objects.all().select_related('policymaker')
        excludes = ['name', 'start_date', 'end_date']
        filtering = {
            'origin_id': ALL,
            'abbreviation': ALL,
            'name': ALL,
        }
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']
        cache = SimpleCache(timeout=CACHE_TIMEOUT)

all_resources = [
    MeetingDocumentResource, PolicymakerResource, CategoryResource,
    MeetingResource, IssueResource, AgendaItemResource, AttachmentResource,
    VideoResource, OrganizationResource
]
