#!/usr/bin/env python3

# Run this script with this command:
#   $ bokeh serve --show ./main.py --args "trace_file"

import sys
import collections
import random
import functools
import traceback
import math
import datetime
import pandas
import numpy
import bokeh.plotting
import bokeh.io
import bokeh.models
import bokeh.models.tickers
import bokeh.layouts
import bokeh.palettes
import bokeh.transform

class TimelineTraceSlice:
    def __init__(self, df):
        self.__df = df
        self.__df.reset_index(inplace=True, drop=True)

    def get_sampled_slice(self, num_samples):
        return TimelineTraceSlice(self.__df.sample(num_samples)
                                  if self.size() > num_samples else self.__df)

    def dataframe(self):
        return self.__df

    def size(self):
        return len(self.__df.index)

    def add_rank_pos(self, num_conc):
        self.__df = self.__df.assign(
            rank0_pos=self.__df.loc[:, 'rank0']+
                      (numpy.mod(self.__df.index, num_conc)+0.5)/num_conc,
            height=1/num_conc)
        self.__df = self.__df.assign(
            rank1_pos=numpy.where(self.__df.loc[:, 'rank0'] == self.__df.loc[:, 'rank1'],
                                  self.__df.loc[:, 'rank0_pos'],
                                  self.__df.loc[:, 'rank1']+0.5))

class TimelineTrace:
    def read_csv(self, input_path):
        COLUMNS = ['rank0', 't0', 'rank1', 't1', 'kind']
        print("Reading CSV...")
        df = pandas.read_csv(input_path, names=COLUMNS)

        print("Preparing data...")
        df['duration'] = df['t1']-df['t0']
        self.__columns = df.columns
        self.__time_min = df['t0'].min()
        self.__time_max = df['t1'].max()

        df.sort_values('t0', inplace=True)
        df.rename_axis('line', inplace=True)

        self.__data = collections.OrderedDict((x for x in df.groupby('kind'))) # workaround
        self.__t1_index = collections.OrderedDict()
        for kind, kind_df in self.__data.items():
            kind_df.reset_index(inplace=True)
            self.__t1_index[kind] = kind_df['t1'].sort_values()

        print("Trace is loaded.")
        return self

    def get_sampled_time_slice(self, time_range, kinds, num_samples):
        print("Making slices...")
        index_start = numpy.fromiter(
            ((t1_ser.searchsorted(time_range[0]).item() if kind in kinds else 0)
             for kind, t1_ser in self.__t1_index.items()), dtype=int)
        index_end = numpy.fromiter(
            ((kind_df['t0'].searchsorted(time_range[1], 'right').item() if kind in kinds else 0)
             for kind, kind_df in self.__data.items()), dtype=int)

        num_list = index_end - index_start
        num_sum_right = num_list.cumsum()
        num_total = num_sum_right[len(num_sum_right)-1] if num_sum_right.size > 0 else 0
        num_sum_left = [(0 if i == 0 else num_sum_right[i-1]) for i in range(len(num_sum_right))]

        sampled_idxs = range(num_total)
        if num_total > num_samples:
            sampled_idxs = random.sample(sampled_idxs, num_samples)
        sampled_idxs = numpy.array(sampled_idxs)
        sampled_idxs.sort()

        si_left  = numpy.searchsorted(sampled_idxs, num_sum_left, 'left')
        si_right = numpy.searchsorted(sampled_idxs, num_sum_right, 'left')
        local_sampled_idxs = [sampled_idxs[l:r]-ns for l, r, ns in zip(si_left, si_right, num_sum_left)]

        sub_data = pandas.concat(df.iloc[idx_start+idxs] for df, idx_start, idxs
                                 in zip(self.__data.values(), index_start, local_sampled_idxs))
        return TimelineTraceSlice(sub_data), num_total

    def get_empty_time_slice(self):
        return TimelineTraceSlice(pandas.DataFrame(columns=self.__columns))

    def get_time_range(self):
        return self.__time_min, self.__time_max

    def get_kinds(self):
        return list(self.__data.keys())

class TimelineTraceViewer:
    __refreshed = { 'main': True, 'sub': True }
    __slider_values = {
        'num_main_bar_samples': 10000,
        'label_rate': 0.0,
        'num_rt_bar_samples': 10000,
        'num_conc': 1
    }
    __active_main_tab = 0

    def __init__(self, trace):
        print("Initializing viewer...")
        self.__trace = trace
        self.__kinds = self.__trace.get_kinds()
        self.__visible_kinds = set(self.__kinds)

        def big_palette(size, palette_func):
            if size < 256:
                return palette_func(size)
            p = palette_func(256)
            colors = []
            for i in range(size):
                idx = int(i * 256.0 / size)
                colors.append(p[idx])
            return colors

        kind_colors = big_palette(len(self.__kinds), bokeh.palettes.viridis)
        color_mapper = bokeh.transform.factor_cmap(
            field_name='kind', factors=self.__kinds, palette=kind_colors)

        TOOLTIPS = [
            ('line', "@line"),
            ("t", "(@t0,@t1)"),
            ("duration", "@duration"),
            ("rank", "(@rank0,@rank1)"),
            ("kind", "@kind")
        ]

        init_time_range = self.__trace.get_time_range()
        self.__main_time_range = init_time_range
        self.__rt_time_range = init_time_range

        MainTabInfo = collections.namedtuple(
            'MainTabInfo', ('fig', 'migration_seg', 'bar_src', 'label_src', 'panel'))

        def make_main_tab(tab_num, backend, title, x_range, y_range):
            fig = bokeh.plotting.figure(
                plot_width=1200, plot_height=800,
                x_range=x_range, y_range=y_range,
                tools='hover,xwheel_zoom,ywheel_zoom,xpan,ypan,reset,crosshair,save,help',
                active_drag='xpan', active_scroll='xwheel_zoom',
                tooltips=TOOLTIPS, output_backend=backend)

            yticker = bokeh.models.tickers.SingleIntervalTicker(interval=1)
            fig.yaxis.ticker = yticker
            fig.ygrid.grid_line_alpha = 1
            fig.ygrid.grid_line_color = 'black'
            fig.ygrid.ticker = yticker

            bar_src, label_src = map(bokeh.models.ColumnDataSource, self.__get_main_data(tab_num))

            fig.hbar(
                y='rank0_pos', left="t0", right="t1", height='height', legend='kind',
                color=color_mapper, hover_color=color_mapper, alpha=0.8, hover_alpha=1.0,
                hover_line_color="firebrick", source=bar_src)

            migration_seg = fig.segment(
                x0='t1', x1='t1', y0='rank0_pos', y1='rank1_pos',
                color=color_mapper, source=bar_src)
            migration_seg.visible = False

            labels = bokeh.models.LabelSet(
                y='rank0_pos', x='t0', text='kind', text_baseline='middle',
                source=label_src, level='glyph', render_mode='canvas')
            fig.add_layout(labels)

            panel = bokeh.models.Panel(child=fig, title=title)

            return MainTabInfo(fig=fig, migration_seg=migration_seg,
                               bar_src=bar_src, label_src=label_src, panel=panel)

        webgl_main_tab = make_main_tab(0, 'webgl', "WebGL", init_time_range, None)
        webgl_main_tab.fig.x_range.on_change('start', self.__on_change_time_range)
        webgl_main_tab.fig.x_range.on_change('end', self.__on_change_time_range)

        svg_main_tab = make_main_tab(
            1, 'svg', "SVG", webgl_main_tab.fig.x_range, webgl_main_tab.fig.y_range)

        self.__main_tabs = (webgl_main_tab, svg_main_tab)
        main_tabs = bokeh.models.Tabs(tabs=[ti.panel for ti in self.__main_tabs])
        main_tabs.on_change('active', self.__on_change_main_tab)

        init_rt_bar_data = self.__get_rangetool_data(init_time_range)
        self.__rt_bar_src = bokeh.models.ColumnDataSource(init_rt_bar_data)

        rt_fig = bokeh.plotting.figure(plot_width=1200, plot_height=150,
                                       toolbar_location=None, output_backend='webgl')

        self.__rt_bar = rt_fig.hbar(y="rank0_pos", left="t0", right="t1", height=0.5,
                                    color=color_mapper, alpha=0.8, source=self.__rt_bar_src)

        range_tool = bokeh.models.RangeTool(x_range=webgl_main_tab.fig.x_range)
        rt_fig.add_tools(range_tool)

        self.__sample_info_div = bokeh.models.widgets.Div(text=self.__get_sample_info())

        def create_slider(name, **kwargs):
            slider = bokeh.models.widgets.Slider(value=self.__slider_values[name], **kwargs)
            slider.on_change('value', functools.partial(self.__on_change_slider, name))
            return slider

        num_main_bar_samples_slider = create_slider('num_main_bar_samples',
            start=1, end=100000, step=1, title="# of samples for main plot")

        label_rate_slider = create_slider('label_rate',
            start=0, end=1, step=0.01, title="Rate for showing labels")

        num_rt_bar_samples_slider = create_slider('num_rt_bar_samples',
            start=1, end=100000, step=1, title="# of samples for sub plot")

        num_conc_slider = create_slider('num_conc',
            start=1, end=30, step=1, title="# of concurrent events")

        migrate_checkbox_group = \
            bokeh.models.widgets.CheckboxGroup(labels=["Show migrations"], active=[])
        migrate_checkbox_group.on_click(self.__on_click_migrate_checkboxes)

        kind_all_button = \
            bokeh.models.widgets.CheckboxButtonGroup(labels=["Show all"], active=[0])
        kind_all_button.on_click(self.__on_click_show_all_kinds)

        self.__kind_checkbox_group = \
            bokeh.models.widgets.CheckboxGroup(
                labels=self.__kinds, active=list(range(len(self.__kinds))))
        self.__kind_checkbox_group.on_click(self.__on_click_kind_checkboxes)

        curdoc = bokeh.io.curdoc()
        row = bokeh.layouts.row
        column = bokeh.layouts.column
        left_layout = column(main_tabs, rt_fig)
        right_layout = column(self.__sample_info_div,
                              num_main_bar_samples_slider, label_rate_slider,
                              num_rt_bar_samples_slider, num_conc_slider,
                              migrate_checkbox_group,
                              kind_all_button, self.__kind_checkbox_group)
        curdoc.add_root(row(left_layout, right_layout))
        curdoc.add_periodic_callback(self.__on_timer, 100)
        print("Viewer is initialized.")

    def __get_main_data(self, tab_num):
        is_active = tab_num == self.__active_main_tab
        bar_sl, num_total = self.__get_sampled_time_slice(
            self.__main_time_range, self.__slider_values['num_main_bar_samples']) \
            if is_active else self.__get_empty_data()
        if is_active:
            self.__num_main_actual_events = num_total

        num_label_samples = math.ceil(bar_sl.size() * self.__slider_values['label_rate'])
        label_sl = bar_sl.get_sampled_slice(num_label_samples)
        return bar_sl.dataframe(), label_sl.dataframe()

    def __get_rangetool_data(self, time_range):
        rangetool_sl, _ = self.__get_sampled_time_slice(
            self.__rt_time_range, self.__slider_values['num_rt_bar_samples'])
        return rangetool_sl.dataframe()

    def __get_sampled_time_slice(self, time_range, num_samples):
        sl, num_total = self.__trace.get_sampled_time_slice(time_range, self.__visible_kinds, num_samples)
        sl.add_rank_pos(self.__slider_values['num_conc'])
        return sl, num_total

    def __get_empty_data(self):
        sl = self.__trace.get_empty_time_slice()
        sl.add_rank_pos(self.__slider_values['num_conc'])
        return sl, 0

    def __update_main_data(self):
        for i, ti in enumerate(self.__main_tabs):
            ti.bar_src.data, ti.label_src.data = \
                map(bokeh.models.ColumnDataSource.from_df, self.__get_main_data(i))
        self.__sample_info_div.text = self.__get_sample_info()

    def __update_rangetool_data(self):
        self.__rt_bar_src.data = \
            bokeh.models.ColumnDataSource.from_df(
                self.__get_rangetool_data(self.__rt_time_range))

    def __get_sample_info(self):
        n_actual = self.__num_main_actual_events
        n_limit = self.__slider_values['num_main_bar_samples']
        if n_actual > n_limit:
            msg = "{:.3f} % sampled".format(n_limit / n_actual * 100)
        else:
            msg = "<strong>accurate</strong>"
        return "# of actual events for main plot: {}<br/>" \
            "({})".format(n_actual, msg)

    def __on_change_time_range(self, attr, old, new):
        if attr == 'start':
            self.__main_time_range = (new, self.__main_time_range[1])
            self.__request_refresh_main()
        elif attr == 'end':
            self.__main_time_range = (self.__main_time_range[0], new)
            self.__request_refresh_main()

    def __on_change_main_tab(self, attr, old, new):
        self.__active_main_tab = new
        self.__request_refresh_main()

    def __on_change_slider(self, name, attr, old, new):
        self.__slider_values[name] = new
        self.__request_refresh_all()

    def __on_click_migrate_checkboxes(self, active_list):
        is_visible = 0 in active_list
        for ti in self.__main_tabs:
            ti.migration_seg.visible = is_visible

    def __on_click_kind_checkboxes(self, active_list):
        self.__visible_kinds = set(self.__kinds[i] for i in active_list)
        self.__request_refresh_all()

    def __on_click_show_all_kinds(self, active_list):
        self.__kind_checkbox_group.active = \
            list(range(len(self.__kinds))) if 0 in active_list else []

    def __request_refresh_main(self):
        self.__refreshed['main'] = False

    def __request_refresh_all(self):
        self.__request_refresh_main()
        self.__refreshed['sub'] = False

    def __on_timer(self):
        def refresh(plot_name, update_func):
            if not self.__refreshed[plot_name]:
                start_time = datetime.datetime.now()
                print("Refreshing {} plot...".format(plot_name))
                update_func()
                self.__refreshed[plot_name] = True
                end_time = datetime.datetime.now()
                print("Refreshed {} plot in {} sec."
                      .format(plot_name, (end_time-start_time).total_seconds()))
        try:
            refresh('main', self.__update_main_data)
            refresh('sub', self.__update_rangetool_data)
        except:
            # See the trace inside the callback for debugging.
            traceback.print_exc()
            raise

trace = TimelineTrace()
if len(sys.argv) < 2:
    sys.exit("{} [CSV path]".format(sys.argv[0]))
trace.read_csv(sys.argv[1])
viewer = TimelineTraceViewer(trace)

