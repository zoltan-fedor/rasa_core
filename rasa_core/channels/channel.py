from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import json
from flask import Blueprint, jsonify, request, Flask, Response
from multiprocessing import Queue
from threading import Thread
from typing import Text, List, Dict, Any, Optional, Callable, Iterable

from rasa_core import utils

try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin


class UserMessage(object):
    """Represents an incoming message.

     Includes the channel the responses should be sent to."""

    DEFAULT_SENDER_ID = "default"

    def __init__(self,
                 text,  # type: Optional[Text]
                 output_channel=None,  # type: Optional[OutputChannel]
                 sender_id=None,  # type: Text
                 parse_data=None  # type: Dict[Text, Any]
                 ):
        # type: (...) -> None

        self.text = text

        if output_channel is not None:
            self.output_channel = output_channel
        else:
            self.output_channel = CollectingOutputChannel()

        if sender_id is not None:
            self.sender_id = sender_id
        else:
            self.sender_id = self.DEFAULT_SENDER_ID

        self.parse_data = parse_data


def register(input_channels,  # type: List[InputChannel]
             app,  # type: Flask
             on_new_message,  # type: Callable[[UserMessage], None]
             route  # type: Text
             ):
    # type: (...) -> None

    for channel in input_channels:
        p = urljoin(route, channel.url_prefix())
        app.register_blueprint(channel.blueprint(on_new_message), url_prefix=p)


def button_to_string(button, idx=0):
    """Create a string representation of a button."""
    return "{idx}: {title} ({val})".format(
            idx=idx + 1, title=button['title'], val=button['payload'])


class InputChannel(object):

    @classmethod
    def name(cls):
        """Every input channel needs a name to identify it."""
        return cls.__name__

    def url_prefix(self):
        return self.name()

    def blueprint(self, on_new_message):
        # type: (Callable[[UserMessage], None])-> None
        """Defines a Flask blueprint.

        The blueprint will be attached to a running flask server and handel
        incoming routes it registered for."""
        raise NotImplementedError(
                "Component listener needs to provide blueprint.")


class OutputChannel(object):
    """Output channel base class.

    Provides sane implementation of the send methods
    for text only output channels."""

    @classmethod
    def name(cls):
        """Every output channel needs a name to identify it."""
        return cls.__name__

    def send_response(self, recipient_id, message):
        # type: (Text, Dict[Text, Any]) -> None
        """Send a message to the client."""

        if message.get("elements"):
            self.send_custom_message(recipient_id, message.get("elements"))

        elif message.get("buttons"):
            self.send_text_with_buttons(recipient_id,
                                        message.get("text"),
                                        message.get("buttons"))
        elif message.get("text"):
            self.send_text_message(recipient_id,
                                   message.get("text"))

        # if there is an image we handle it separately as an attachment
        if message.get("image"):
            self.send_image_url(recipient_id, message.get("image"))

        if message.get("attachment"):
            self.send_attachment(recipient_id, message.get("attachment"))

    def send_text_message(self, recipient_id, message):
        # type: (Text, Text) -> None
        """Send a message through this channel."""

        raise NotImplementedError("Output channel needs to implement a send "
                                  "message for simple texts.")

    def send_image_url(self, recipient_id, image_url):
        # type: (Text, Text) -> None
        """Sends an image. Default will just post the url as a string."""

        self.send_text_message(recipient_id, "Image: {}".format(image_url))

    def send_attachment(self, recipient_id, attachment):
        # type: (Text, Text) -> None
        """Sends an attachment. Default will just post as a string."""

        self.send_text_message(recipient_id,
                               "Attachment: {}".format(attachment))

    def send_text_with_buttons(self, recipient_id, message, buttons, **kwargs):
        # type: (Text, Text, List[Dict[Text, Any]], Any) -> None
        """Sends buttons to the output.

        Default implementation will just post the buttons as a string."""

        self.send_text_message(recipient_id, message)
        for idx, button in enumerate(buttons):
            button_msg = button_to_string(button, idx)
            self.send_text_message(recipient_id, button_msg)

    def send_custom_message(self, recipient_id, elements):
        # type: (Text, Iterable[Dict[Text, Any]]) -> None
        """Sends elements to the output.

        Default implementation will just post the elements as a string."""

        for element in elements:
            element_msg = "{title} : {subtitle}".format(
                    title=element['title'], subtitle=element['subtitle'])
            self.send_text_with_buttons(
                    recipient_id, element_msg, element['buttons'])


class CollectingOutputChannel(OutputChannel):
    """Output channel that collects send messages in a list

    (doesn't send them anywhere, just collects them)."""

    def __init__(self):
        self.messages = []

    @classmethod
    def name(cls):
        return "collector"

    @staticmethod
    def _message(recipient_id,
                 text=None,
                 image=None,
                 buttons=None,
                 attachment=None):
        """Create a message object that will be stored."""

        obj = {
            "recipient_id": recipient_id,
            "text": text,
            "image": image,
            "buttons": buttons,
            "attachment": attachment
        }

        # filter out any values that are `None`
        return utils.remove_none_values(obj)

    def latest_output(self):
        if self.messages:
            return self.messages[-1]
        else:
            return None

    def _persist_message(self, message):
        self.messages.append(message)

    def send_text_message(self, recipient_id, message):
        for message_part in message.split("\n\n"):
            self._persist_message(self._message(recipient_id,
                                                text=message_part))

    def send_text_with_buttons(self, recipient_id, message, buttons, **kwargs):
        self._persist_message(self._message(recipient_id,
                                            text=message,
                                            buttons=buttons))

    def send_image_url(self, recipient_id, image_url):
        # type: (Text, Text) -> None
        """Sends an image. Default will just post the url as a string."""

        self._persist_message(self._message(recipient_id,
                                            image=image_url))

    def send_attachment(self, recipient_id, attachment):
        # type: (Text, Text) -> None
        """Sends an attachment. Default will just post as a string."""

        self._persist_message(self._message(recipient_id,
                                            attachment=attachment))


class QueueOutputChannel(CollectingOutputChannel):
    """Output channel that collects send messages in a list

    (doesn't send them anywhere, just collects them)."""

    @classmethod
    def name(cls):
        return "queue"

    def __init__(self, message_queue=None):
        # type: (Queue) -> None
        self.messages = Queue() if not message_queue else message_queue

    def _persist_message(self, message):
        self.messages.put(message)


class RestInput(InputChannel):
    """A custom http input channel.

    This implementation is the basis for a custom implementation of a chat
    frontend. You can customize this to send messages to Rasa Core and
    retrieve responses from the agent."""

    @classmethod
    def name(cls):
        return "rest"

    @staticmethod
    def on_message_wrapper(on_new_message, text, queue, sender_id):
        collector = QueueOutputChannel(queue)

        message = UserMessage(text, collector, sender_id)
        on_new_message(message)

        queue.put("DONE")

    def _extract_sender(self, req):
        return req.json.get("sender", None)

    # noinspection PyMethodMayBeStatic
    def _extract_message(self, req):
        return req.json.get("message", None)

    def stream_response(self, on_new_message, text, sender_id):
        from multiprocessing import Queue

        q = Queue()

        t = Thread(target=self.on_message_wrapper,
                   args=(on_new_message, text, q, sender_id))
        t.start()
        while True:
            response = q.get()
            if response == "DONE":
                break
            else:
                yield json.dumps(response) + "\n"

    def blueprint(self, on_new_message):
        custom_webhook = Blueprint('custom_webhook', __name__)

        @custom_webhook.route("/", methods=['GET'])
        def health():
            return jsonify({"status": "ok"})

        @custom_webhook.route("/webhook", methods=['POST'])
        def receive():
            sender_id = self._extract_sender(request)
            text = self._extract_message(request)
            should_use_stream = utils.bool_arg("stream", default=False)

            if should_use_stream:
                return Response(
                        self.stream_response(on_new_message, text, sender_id),
                        content_type='text/event-stream')
            else:
                collector = CollectingOutputChannel()
                on_new_message(UserMessage(text, collector, sender_id))
                return jsonify(collector.messages)

        return custom_webhook
