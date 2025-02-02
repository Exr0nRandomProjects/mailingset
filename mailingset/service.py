# Mailing Set: set-algebraic operations on mailing lists
# Copyright (C) 2015 by David Tolnay <dtolnay@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""The Twisted classes for the Mailing Set SMTP server.

Mailing Set can be added to a Twisted application using the following code:

    factory = SetSMTPFactory(config, sendmail)
    service = internet.TCPServer(port, factory)
    service.setServiceParent(application)

"""
from __future__ import absolute_import
from builtins import str
from builtins import object
import email
from email import parser
import netaddr

from zope.interface import implementer

from twisted.mail import smtp
from twisted.python import log

from .mailman import subject_prefix

from .state import MailingSetState
from . import parser


__all__ = ['SetSMTPFactory']


class SetSMTPFactory(smtp.SMTPFactory):

    def __init__(self, config, sendmail, *a, **kw):
        """
        Args:
            config: ConfigParser object holding configuration for the Mailing
                Set SMTP server.
            sendmail: A function with the same signature as smtp.sendmail which
                will be called to send outgoing messages. Test code uses this to
                check assertions on the outgoing messages. Production code
                passes in smtp.sendmail itself.
        """
        smtp.SMTPFactory.__init__(self, *a, **kw)

        self.config = config
        self.sendmail = sendmail

        # Cache list definitions and use them to parse destination addresses
        resolver = MailingSetState(self.config)
        self.parse = lambda address: parser.parse(resolver, address)

    def buildProtocol(self, addr):
        """Builds the protocol governing the connection to the given address.

        Specified by IProtocolFactory interface.

        Args:
            addr: The (host,port) pair of the newly established connection. Not
                used by this factory because all connections use the same
                protocol.

        Returns:
            The protocol, an implementation of IProtocol.
        """
        protocol = smtp.ESMTP()
        protocol.delivery = SetMessageDelivery(protocol, self.config,
                self.parse, self.sendmail)
        return protocol


@implementer(smtp.IMessageDelivery)
class SetMessageDelivery(object):

    def __init__(self, protocol, config, parse, sendmail):
        """
        Args:
            protocol: The protocol governing interaction with client
                connections.
            config: ConfigParser object holding configuration for the Mailing
                Set SMTP server.
            parse: A function taking an email address and returning a pair of
                subject tag and recipient address set.
            sendmail: A function with the same signature as smtp.sendmail which
                will be called to send outgoing messages.
        """
        self.protocol = protocol
        self.config = config
        self.parse = parse
        self.sendmail = sendmail

    def receivedHeader(self, helo, origin, recipients):
        """Generates the Received header for a message.

        Specified by IMessageDelivery interface.

        Args:
            helo: The argument to the HELO command and the client's IP address.
            origin: The address the message is from.
            recipients: A list of the addresses for which this message is bound.

        Returns:
            The full "Received" header string.
        """
        client_hostname, _ = helo
        server_hostname = self.protocol.transport.getHost().host
        header_value = b'from %s by %s with ESMTP ; %s' % (
            client_hostname, server_hostname.encode(), smtp.rfc822date())
        return 'Received: %s' % (email.header.Header(header_value),)

    def validateFrom(self, helo, origin):
        """Validate the address from which the message originates.

        Specified by IMessageDelivery interface.

        Args:
            helo: The argument to the HELO command and the client's IP address.
            origin: The address the message is from.

        Returns:
            Just origin.

        Raises:
            SMTPBadSender: If origin is not one of the accept_from addresses set
                in the server config.
        """
        good = self.config.get('incoming', 'accept_from', fallback='0.0.0.0/0')
        for cidr in good.split(','):
            client_ip = helo[1].decode() if isinstance(helo[1], bytes) else helo[1] # twisted uses bytes for addresses, but netaddr automatically uses strings when it detects python3, so we should convert to string here. ~@exr0n jan024

            if client_ip in netaddr.IPNetwork(cidr):
                # Accept messages from this address
                log.msg('Receiving from %s %s' % (helo, origin))
                return origin

        # Do not accept messages from this address
        log.msg('Rejecting from %s %s' % (helo, origin))
        raise smtp.SMTPBadSender(helo[1])

    def validateTo(self, user):
        """Validate the address for which the message is destined.

        Specified by IMessageDelivery interface.

        Args:
            user: The address to validate.

        Returns:
            A callable which takes no arguments and returns an object
            implementing IMessage, which will be used to deliver the message
            when it arrives.

        Raises:
            SMTPBadRcpt: If the domain of the recipient address does not match
                the server's domain, or if the recipient address fails to parse
                as a set expression. This results in a bounce back to the
                sender.
        """
        # Check for domain matching server's domain
        domain = user.dest.domain
        if domain != self.config.get('incoming', 'domain').encode():
            log.msg('Rejecting domain %s' % (domain,))
            reason = 'Incorrect domain: %s' % (domain,)
            raise smtp.SMTPBadRcpt(user, resp=reason)

        # Try to parse address as set expression
        local = user.dest.local
        try:
            subject_tag, recipient_set = self.parse(local.decode())
        except SyntaxError as error:
            log.msg('Rejecting address %s: %s' % (local, error))
            reason = str(error)
            raise smtp.SMTPBadRcpt(user, resp=reason)

        # Good to go, receive rest of message
        return lambda: SetMessage(
                self.config, local, subject_tag, recipient_set, self.sendmail)


@implementer(smtp.IMessage)
class SetMessage(object):

    def __init__(self, config, address, subject_tag, recipient_set, sendmail):
        """
        Args:
            config: ConfigParser object holding configuration for the Mailing
                Set SMTP server.
            address: The original recipient address of the message.
            subject_tag: Tag that will be prepended in square brackets to the
                message subject to indicate the target set expression.
            recipient_set: The actual recipient addresses as a set of strings.
            sendmail: A function with the same signature as smtp.sendmail which
                will be called to send outgoing messages.
        """
        self.config = config
        self.address = address
        self.subject_tag = subject_tag
        self.recipient_set = recipient_set
        self.sendmail = sendmail

        # Buffer to receive rest of message
        self.msg_parser = email.parser.FeedParser()

    def lineReceived(self, line):
        """Handles another line of data.

        Specified by IMessage interface.

        Args:
            line: Line of message data without terminating newline.
        """
        self.msg_parser.feed(line)
        self.msg_parser.feed('\n')

    def eomReceived(self):
        """Handles the end of the message.

        Fixes up the message headers and sends it through the outgoing server to
        the appropriate recipients, including the archival address if one is
        present in the server config.

        Specified by IMessage interface.

        Returns:
            A Deferred responsible for sending the message through the outgoing
            server.
        """
        msg = self.msg_parser.close()
        self.msg_parser = None

        # Prepend subject tag and set mailing list headers
        self._munge_header(msg)

        # Add archival address to recipient set if there is one
        recp = self.recipient_set
        if self.config.has_option('outgoing', 'archive_addr'):
            recp |= set([self.config.get('outgoing', 'archive_addr')])

        # Log
        log.msg('Subject: %s' % (str(msg['Subject']),))
        log.msg('Sending to: %s' % (', '.join(recp),))

        # Get outgoing config
        outgoing_server = self.config.get('outgoing', 'server')
        outgoing_port = self.config.getint('outgoing', 'port')
        envelope_sender = self.config.get('outgoing', 'envelope_sender')

        # Begin sending the message!
        send = self.sendmail(outgoing_server, envelope_sender, recp,
                msg.as_string(), port=outgoing_port)
        send.addCallback(log.msg, 'Success %s' % (self.address,))
        send.addErrback(log.err, 'Failure %s' % (self.address,))
        return send

    def connectionLost(self):
        """Handles truncation of message by discarding anything received so far.

        Specified by IMessage interface.
        """
        log.err('Connection lost %s' % (self.address,))
        self.msg_parser = None

    def _munge_header(self, msg):
        """Prepends subject tag and sets mailing list headers.

        Args:
            msg: The email.message.Message object whose headers to modify.
        """
        # Prepend subject tag if not already present
        tag = '[%s] ' % (self.subject_tag,)
        try:
            subject_prefix.SubjectPrefix().process(tag, msg)
        except (UnicodeError, ValueError):
            pass

        # set Precedence header to identify message as mailing list traffic
        if 'precedence' not in msg:
            msg['Precedence'] = 'list'

        # List-* headers
        domain = self.config.get('incoming', 'domain')
        del msg['list-id']
        msg['List-Id'] = '<%s.mailingset.%s>' % (self.address, domain)
        del msg['list-post']
        msg['List-Post'] = '<mailto:%s@%s>' % (self.address, domain)
