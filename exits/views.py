from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone

from payroll.views import company_view

from . import services
from .forms import ExitForm, ItemForm, LineForm
from .models import ClearanceItem, Exit, SettlementLine


def _exit(request, pk):
    return get_object_or_404(Exit.objects.select_related("employee"), pk=pk, company=request.company)


def _back(ex):
    return redirect("exits:detail", ex.pk)


@company_view
def overview(request):
    exits = (Exit.objects.filter(company=request.company).select_related("employee")
             .annotate(total=Count("items"), done=Count("items", filter=Q(items__is_done=True)))
             .order_by("status", "last_working_day"))
    return render(request, "exits/overview.html", {"exits": exits})


@company_view
def new(request):
    form = ExitForm(request.POST or None, instance=Exit(company=request.company), company=request.company)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        ex = services.start_exit(d["employee"], d["reason"], d["last_working_day"], d["notice_date"], d["notes"],
                                 request.user)
        messages.success(request, f"Exit started for {ex.employee}. Pay is pro-rated in the final month's payroll.")
        return _back(ex)
    return render(request, "exits/form.html", {"title": "Start an exit", "form": form,
                                               "back_url": reverse("exits:overview")})


@company_view
def detail(request, pk):
    ex = _exit(request, pk)
    locked = services.is_locked(ex)
    run = services.final_run(ex)
    ctx = {"ex": ex, "locked": locked, "items": ex.items.all(), "lines": ex.lines.all(), "run": run,
           "item_form": ItemForm(), "blockers": services.blockers(ex) if ex.status == Exit.Status.OPEN else [],
           "run_needs_regenerating": bool(ex.status == Exit.Status.OPEN and run and run.status != "DRAFT"
                                          and not locked),
           "payouts": services.leave_payouts(ex) if ex.status == Exit.Status.OPEN and not locked else []}
    if ex.status == Exit.Status.OPEN:
        ctx["preview"] = services.preview(ex)
    return render(request, "exits/detail.html", ctx)


@company_view
def edit(request, pk):
    ex = _exit(request, pk)
    if services.is_locked(ex):
        messages.error(request, "The final month has been approved, so the dates can no longer be changed.")
        return _back(ex)
    form = ExitForm(request.POST or None, instance=ex, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Exit updated.")
        return _back(ex)
    return render(request, "exits/form.html", {"title": f"Edit exit: {ex.employee}", "form": form,
                                               "back_url": reverse("exits:detail", args=[ex.pk])})


@company_view
@require_POST
def complete(request, pk):
    ex = _exit(request, pk)
    try:
        services.complete(ex, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{ex.employee} has left. They are no longer on payroll.")
    return _back(ex)


@company_view
@require_POST
def cancel(request, pk):
    ex = _exit(request, pk)
    try:
        services.cancel(ex)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Exit cancelled. Pay continues as normal.")
    return _back(ex)


# ---- clearance checklist: open to ticking until the exit is completed
@company_view
@require_POST
def item_add(request, pk):
    ex = _exit(request, pk)
    form = ItemForm(request.POST)
    if ex.status != Exit.Status.OPEN:
        messages.error(request, "This exit is no longer in progress.")
    elif form.is_valid():
        ClearanceItem.objects.create(exit=ex, name=form.cleaned_data["name"])
    else:
        messages.error(request, "Enter a name for the item.")
    return _back(ex)


@company_view
@require_POST
def item_toggle(request, pk):
    item = get_object_or_404(ClearanceItem.objects.select_related("exit__employee"), pk=pk,
                             exit__company=request.company)
    if item.exit.status != Exit.Status.OPEN:
        messages.error(request, "This exit is no longer in progress.")
    else:
        item.is_done = not item.is_done
        item.done_by, item.done_at = (request.user, timezone.now()) if item.is_done else (None, None)
        item.save()
    return _back(item.exit)


@company_view
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(ClearanceItem.objects.select_related("exit"), pk=pk, exit__company=request.company)
    if item.exit.status == Exit.Status.OPEN:
        item.delete()
    return _back(item.exit)


# ---- settlement lines: locked once the final month is approved
@company_view
def line_edit(request, pk=None, exit_pk=None):
    if pk:
        line = get_object_or_404(SettlementLine.objects.select_related("exit__employee"), pk=pk,
                                 exit__company=request.company)
        ex = line.exit
    else:
        ex = _exit(request, exit_pk)
        line = SettlementLine(exit=ex)
    if services.is_locked(ex):
        messages.error(request, "The final month has been approved, so the settlement can no longer be changed.")
        return _back(ex)
    form = LineForm(request.POST or None, instance=line)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settlement line saved. Generate the payroll run for that month again to include it.")
        return _back(ex)
    return render(request, "exits/form.html", {"title": f"{'Edit' if pk else 'Add'} settlement line for {ex.employee}",
                                               "form": form, "back_url": reverse("exits:detail", args=[ex.pk])})


@company_view
@require_POST
def line_delete(request, pk):
    line = get_object_or_404(SettlementLine.objects.select_related("exit"), pk=pk, exit__company=request.company)
    if services.is_locked(line.exit):
        messages.error(request, "The final month has been approved, so the settlement can no longer be changed.")
    else:
        line.delete()
        messages.success(request, "Line removed.")
    return _back(line.exit)


@company_view
@require_POST
def suggest(request, pk):
    ex = _exit(request, pk)
    if services.is_locked(ex):
        messages.error(request, "The final month has been approved, so the settlement can no longer be changed.")
        return _back(ex)
    added = services.add_suggestions(ex)
    messages.success(request, f"Added {len(added)} line(s). Check the amounts before relying on them."
                     if added else "Nothing to suggest: there is no unused leave to pay out, or it is already listed.")
    return _back(ex)
