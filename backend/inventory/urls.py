from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory import views

router = DefaultRouter()
router.register("strata", views.StratumViewSet)
router.register("species", views.SpeciesViewSet)
router.register("campaigns", views.CampaignViewSet)
router.register("equations", views.EquationViewSet)
router.register("plots", views.PlotViewSet)
router.register("trees", views.TreeViewSet, basename="tree")
router.register("measurements", views.MeasurementViewSet,
                basename="measurement")
router.register("conflicts", views.ConflictViewSet, basename="conflict")
router.register("imports", views.ImportViewSet, basename="import")
router.register("surveys", views.SurveyViewSet, basename="survey")
router.register("batches", views.BatchViewSet, basename="batch")
router.register("batch-conflicts", views.BatchConflictViewSet,
                basename="batch-conflict")
router.register("import-rows", views.ImportRowViewSet, basename="import-row")
router.register("estimates", views.EstimateViewSet, basename="estimate")

urlpatterns = [path("", include(router.urls))]
