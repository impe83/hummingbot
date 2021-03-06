#!/usr/bin/env python

from os.path import join, realpath, dirname
import sys;sys.path.insert(0, realpath(join(__file__, "../../../")))

from prompt_toolkit.layout.containers import VSplit, HSplit, Window, FloatContainer, Float
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer
from hummingbot.cli.ui.custom_widgets import CustomTextArea as TextArea


HEADER = """
                                                   *,.                     
                                                 *,,,*                     
                                                ,,,,,,,               *    
                                              ,,,,,,,,            ,,,,     
                                             *,,,,,,,,(        .,,,,,,     
                                           /,,,,,,,,,,     .*,,,,,,,,      
                                          .,,,,,,,,,,,.  ,,,,,,,,,,,*      
                                         ,,,,,,,,,,,,,,,,,,,,,,,,,,,       
                               //      ,,,,,,,,,,,,,,,,,,,,,,,,,,,,#*%     
                           .,,,,,,,,. *,,,,,,,,,,,,,,,,,,,,,,,,,,,%%%%%%&@ 
                         ,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,%%%%%%%&  
                       ,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,%%%%%%%&   
                     /*,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,(((((%%&     
                  **.         #,,,,,,,,,,,,,,,,,,,,,,,,,,,,,((((((((((#.   
               **               *,,,,,,,,,,,,,,,,,,,,,,,,**/(((((((((((((* 
                                  ,,,,,,,,,,,,,,,,,,,,*********((((((((((((
                                    ,,,,,,,,,,,,,,,**************((((((((@ 
                                    (,,,,,,,,,,,,,,,***************(#      
                                     *,,,,,,,,,,,,,,,,**************/      
                                       ,,,,,,,,,,,,,,,***************/     
                                         ,,,,,,,,,,,,,,****************    
                                           .,,,,,,,,,,,,**************/    
                                                ,,,,,,,,*******,           
                                               *,,,,,,,,********           
                                               ,,,,,,,,,/******/           
                                              ,,,,,,,,,@  /****/           
                                             ,,,,,,,,                      
                                             , */  


    ██╗  ██╗██╗   ██╗███╗   ███╗███╗   ███╗██╗███╗   ██╗ ██████╗ ██████╗  ██████╗ ████████╗
    ██║  ██║██║   ██║████╗ ████║████╗ ████║██║████╗  ██║██╔════╝ ██╔══██╗██╔═══██╗╚══██╔══╝
    ███████║██║   ██║██╔████╔██║██╔████╔██║██║██╔██╗ ██║██║  ███╗██████╔╝██║   ██║   ██║   
    ██╔══██║██║   ██║██║╚██╔╝██║██║╚██╔╝██║██║██║╚██╗██║██║   ██║██╔══██╗██║   ██║   ██║   
    ██║  ██║╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║██║██║ ╚████║╚██████╔╝██████╔╝╚██████╔╝   ██║   
    ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚═════╝  ╚═════╝    ╚═╝   

================================================================================================
Press CTRL + C to quit at any time.
Enter "help" for a list of commands.
"""

with open(join(dirname(__file__), '../../VERSION')) as version_file:
    version = version_file.read().strip()


def create_input_field(lexer=None, completer: Completer = None):
    return TextArea(
        height=10,
        prompt='>>> ',
        style='class:input-field',
        multiline=False,
        focus_on_click=True,
        lexer=lexer,
        auto_suggest=AutoSuggestFromHistory(),
        completer=completer,
        complete_while_typing=True,
    )


def create_output_field():
    return TextArea(
        style='class:output-field',
        focus_on_click=False,
        read_only=False
    )


def create_log_field():
    return TextArea(
        style='class:log-field',
        text="Running logs\n",
        focus_on_click=False,
        read_only=False
    )


def generate_layout(input_field: TextArea, output_field: TextArea, log_field: TextArea):
    root_container = VSplit([
        FloatContainer(
            HSplit([
                output_field,
                Window(height=1, char='-', style='class:line'),
                input_field,
                TextArea(height=1,
                         text=f'Version: {version}    [Ctrl + C] QUIT    Hold down "fn" for selecting and copying text',
                         style='class:label'),
            ]),
            [
                # Completion menus.
                Float(xcursor=True,
                      ycursor=True,
                      transparent=True,
                      content=CompletionsMenu(
                          max_height=16,
                          scroll_offset=1)),
            ]
        ),
        Window(width=1, char='|', style='class:line'),
        log_field,
    ])
    return Layout(root_container, focused_element=input_field)

